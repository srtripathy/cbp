from __future__ import annotations

import os
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PLAYERS: list[str] = []
GAMES_PER_WEEK = 16
PLAYERS_PER_GAME = 4

CLUB_NAME = os.environ.get("CLUB_NAME", "Badminton Week Sheet")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

metadata = MetaData()

players_table = Table(
    "players",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False, unique=True),
    Column("sort_order", Integer, nullable=False),
)

weeks_table = Table(
    "weeks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("week_date", Date, nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False),
)

week_player_games_table = Table(
    "week_player_games",
    metadata,
    Column("week_id", Integer, ForeignKey("weeks.id", ondelete="CASCADE"), primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id", ondelete="CASCADE"), primary_key=True),
    Column("game_no", Integer, primary_key=True),
    Column("played", Boolean, nullable=False, default=False),
)

next_up_slots_table = Table(
    "next_up_slots",
    metadata,
    Column("week_id", Integer, ForeignKey("weeks.id", ondelete="CASCADE"), primary_key=True),
    Column("slot_no", Integer, primary_key=True),
    Column("player_id", Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False),
)


def build_engine():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sqlite_path = os.environ.get("SQLITE_PATH")
        if sqlite_path:
            db_path = Path(sqlite_path)
        else:
            db_path = APP_DIR / "badminton.db"
        return create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url, pool_pre_ping=True)


engine = build_engine()


def init_db() -> None:
    metadata.create_all(engine)

    with engine.begin() as conn:
        result = conn.execute(select(func.count()).select_from(players_table)).scalar_one()
        if result == 0 and DEFAULT_PLAYERS:
            conn.execute(
                insert(players_table),
                [
                    {"name": name, "sort_order": idx}
                    for idx, name in enumerate(DEFAULT_PLAYERS, start=1)
                ],
            )



def ensure_week(week_date: date) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            select(weeks_table.c.id).where(weeks_table.c.week_date == week_date)
        ).first()
        if row:
            return int(row[0])

        result = conn.execute(
            insert(weeks_table).values(week_date=week_date, created_at=datetime.utcnow())
        )
        week_id = int(result.inserted_primary_key[0])

        players = conn.execute(
            select(players_table.c.id).order_by(players_table.c.sort_order.asc())
        ).fetchall()

        inserts = []
        for player_id_row in players:
            player_id = int(player_id_row[0])
            for game_no in range(1, GAMES_PER_WEEK + 1):
                inserts.append(
                    {
                        "week_id": week_id,
                        "player_id": player_id,
                        "game_no": game_no,
                        "played": False,
                    }
                )

        if inserts:
            conn.execute(insert(week_player_games_table), inserts)

        return week_id



def row_to_dict(row) -> Dict[str, Any]:
    if row is None:
        return {}
    try:
        return dict(row._mapping)
    except AttributeError:
        # Fallback for tuple rows
        return dict(row)


def get_weeks() -> List[Dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(select(weeks_table).order_by(weeks_table.c.week_date.desc())).fetchall()
        return [row_to_dict(r) for r in rows]


def build_player_totals(
    rows: List[Tuple[int, int, bool]], players: List[Dict[str, Any]]
) -> Dict[int, int]:
    player_totals: Dict[int, int] = {int(p["id"]): 0 for p in players}
    for player_id, _game_no, played in rows:
        if played:
            player_totals[int(player_id)] += 1
    return player_totals


def suggested_next_up(
    players: List[Dict[str, Any]], player_totals: Dict[int, int]
) -> List[Dict[str, Any]]:
    if len(players) <= 4:
        return players

    ranked_players = sorted(
        players,
        key=lambda player: (
            player_totals.get(int(player["id"]), 0),
            int(player["sort_order"]),
            player["name"].lower(),
        ),
    )
    return ranked_players[:4]


def saved_next_up_for_week(week_id: int) -> List[int]:
    with engine.begin() as conn:
        rows = conn.execute(
            select(next_up_slots_table.c.player_id)
            .where(next_up_slots_table.c.week_id == week_id)
            .order_by(next_up_slots_table.c.slot_no.asc())
        ).fetchall()
    return [int(row[0]) for row in rows]


def replace_next_up_for_week(week_id: int, player_ids: List[int]) -> None:
    with engine.begin() as conn:
        conn.execute(delete(next_up_slots_table).where(next_up_slots_table.c.week_id == week_id))
        if player_ids:
            conn.execute(
                insert(next_up_slots_table),
                [
                    {"week_id": week_id, "slot_no": slot_no, "player_id": player_id}
                    for slot_no, player_id in enumerate(player_ids, start=1)
                ],
            )



def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if not ADMIN_PASSWORD:
        return render_template(
            "login.html",
            club_name=CLUB_NAME,
            error="ADMIN_PASSWORD is not set. Configure it in your environment.",
        )

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and ADMIN_PASSWORD and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))

        return render_template(
            "login.html",
            club_name=CLUB_NAME,
            error="Invalid login. Please try again.",
        )

    return render_template("login.html", club_name=CLUB_NAME, error=None)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    init_db()
    weeks = get_weeks()
    if not weeks:
        return render_template("empty.html", club_name=CLUB_NAME)

    return redirect(url_for("week_view", week_id=weeks[0]["id"]))


@app.route("/week/<int:week_id>")
@login_required
def week_view(week_id: int):
    init_db()

    with engine.begin() as conn:
        week_row = conn.execute(select(weeks_table).where(weeks_table.c.id == week_id)).first()
        week = row_to_dict(week_row)
        if not week:
            return redirect(url_for("index"))

        players_rows = conn.execute(
            select(players_table).order_by(players_table.c.sort_order.asc())
        ).fetchall()
        players = [row_to_dict(r) for r in players_rows]

        rows = conn.execute(
            select(
                week_player_games_table.c.player_id,
                week_player_games_table.c.game_no,
                week_player_games_table.c.played,
            ).where(week_player_games_table.c.week_id == week_id)
        ).fetchall()

    played_map = {(r[0], r[1]): r[2] for r in rows}
    player_totals = build_player_totals(rows, players)
    game_counts: Dict[int, int] = {game_no: 0 for game_no in range(1, GAMES_PER_WEEK + 1)}
    for _player_id, game_no, played in rows:
        if played:
            game_counts[int(game_no)] += 1
    weeks = get_weeks()

    return render_template(
        "week.html",
        club_name=CLUB_NAME,
        week=week,
        weeks=weeks,
        players=players,
        games=range(1, GAMES_PER_WEEK + 1),
        played_map=played_map,
        player_totals=player_totals,
        game_counts=game_counts,
        players_per_game=PLAYERS_PER_GAME,
    )


@app.route("/next-up/<int:week_id>", methods=["GET", "POST"])
@login_required
def next_up(week_id: int):
    init_db()

    with engine.begin() as conn:
        week_row = conn.execute(select(weeks_table).where(weeks_table.c.id == week_id)).first()
        week = row_to_dict(week_row)
        if not week:
            return redirect(url_for("index"))

        players_rows = conn.execute(
            select(players_table).order_by(players_table.c.sort_order.asc())
        ).fetchall()
        players = [row_to_dict(r) for r in players_rows]

        game_rows = conn.execute(
            select(
                week_player_games_table.c.player_id,
                week_player_games_table.c.game_no,
                week_player_games_table.c.played,
            ).where(week_player_games_table.c.week_id == week_id)
        ).fetchall()

    player_totals = build_player_totals(game_rows, players)
    suggested_players = suggested_next_up(players, player_totals)
    player_by_id = {int(player["id"]): player for player in players}

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "reset":
            replace_next_up_for_week(week_id, [])
            return redirect(url_for("next_up", week_id=week_id))

        selected_ids: List[int] = []
        for slot_no in range(1, 5):
            raw_value = request.form.get(f"slot_{slot_no}", "").strip()
            if not raw_value:
                continue
            try:
                player_id = int(raw_value)
            except ValueError:
                continue
            if player_id not in player_by_id or player_id in selected_ids:
                continue
            selected_ids.append(player_id)

        replace_next_up_for_week(week_id, selected_ids)
        return redirect(url_for("next_up", week_id=week_id))

    saved_ids = saved_next_up_for_week(week_id)
    if saved_ids:
        current_players = [player_by_id[player_id] for player_id in saved_ids if player_id in player_by_id]
    else:
        current_players = suggested_players

    current_slots: List[Dict[str, Any] | None] = list(current_players[:4])
    while len(current_slots) < 4:
        current_slots.append(None)

    weeks = get_weeks()
    return render_template(
        "next_up.html",
        club_name=CLUB_NAME,
        week=week,
        weeks=weeks,
        players=players,
        player_totals=player_totals,
        suggested_players=suggested_players,
        current_slots=current_slots,
    )


@app.route("/players", methods=["GET", "POST"])
@login_required
def players():
    init_db()

    if request.method == "POST":
        raw = request.form.get("players", "")
        names = [line.strip() for line in raw.splitlines() if line.strip()]

        # Remove duplicates while preserving order
        seen = set()
        unique_names = []
        for name in names:
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            unique_names.append(name)

        if unique_names:
            with engine.begin() as conn:
                # Reset players and games for all weeks
                conn.execute(delete(next_up_slots_table))
                conn.execute(delete(week_player_games_table))
                conn.execute(delete(players_table))

                conn.execute(
                    insert(players_table),
                    [
                        {"name": name, "sort_order": idx}
                        for idx, name in enumerate(unique_names, start=1)
                    ],
                )

                weeks = conn.execute(select(weeks_table.c.id)).fetchall()
                players_rows = conn.execute(select(players_table.c.id)).fetchall()
                inserts = []
                for week_row in weeks:
                    week_id = int(week_row[0])
                    for player_row in players_rows:
                        player_id = int(player_row[0])
                        for game_no in range(1, GAMES_PER_WEEK + 1):
                            inserts.append(
                                {
                                    "week_id": week_id,
                                    "player_id": player_id,
                                    "game_no": game_no,
                                    "played": False,
                                }
                            )

                if inserts:
                    conn.execute(insert(week_player_games_table), inserts)

            return redirect(url_for("players"))

    with engine.begin() as conn:
        players_rows = conn.execute(
            select(players_table).order_by(players_table.c.sort_order.asc())
        ).fetchall()

    players_list = [row_to_dict(r) for r in players_rows]
    players_text = "\n".join([p["name"] for p in players_list])

    return render_template(
        "players.html",
        club_name=CLUB_NAME,
        players_text=players_text,
    )


@app.route("/create_week", methods=["POST"])
@login_required
def create_week():
    init_db()
    date_str = request.form.get("week_date") or date.today().isoformat()
    week_date = datetime.fromisoformat(date_str).date()
    week_id = ensure_week(week_date)
    return redirect(url_for("week_view", week_id=week_id))


@app.route("/toggle", methods=["POST"])
@login_required
def toggle():
    init_db()
    data = request.get_json(force=True)
    week_id = int(data["week_id"])
    player_id = int(data["player_id"])
    game_no = int(data["game_no"])

    with engine.begin() as conn:
        row = conn.execute(
            select(week_player_games_table.c.played).where(
                (week_player_games_table.c.week_id == week_id)
                & (week_player_games_table.c.player_id == player_id)
                & (week_player_games_table.c.game_no == game_no)
            )
        ).first()

        if not row:
            return jsonify({"ok": False}), 404

        new_value = not row[0]
        if new_value:
            selected_count = conn.execute(
                select(func.count())
                .select_from(week_player_games_table)
                .where(
                    (week_player_games_table.c.week_id == week_id)
                    & (week_player_games_table.c.game_no == game_no)
                    & (week_player_games_table.c.played.is_(True))
                )
            ).scalar_one()
            if int(selected_count) >= PLAYERS_PER_GAME:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"Game {game_no} already has {PLAYERS_PER_GAME} players selected.",
                            "limit_reached": True,
                        }
                    ),
                    409,
                )

        conn.execute(
            update(week_player_games_table)
            .where(
                (week_player_games_table.c.week_id == week_id)
                & (week_player_games_table.c.player_id == player_id)
                & (week_player_games_table.c.game_no == game_no)
            )
            .values(played=new_value)
        )
        selected_count = conn.execute(
            select(func.count())
            .select_from(week_player_games_table)
            .where(
                (week_player_games_table.c.week_id == week_id)
                & (week_player_games_table.c.game_no == game_no)
                & (week_player_games_table.c.played.is_(True))
            )
        ).scalar_one()

    return jsonify(
        {
            "ok": True,
            "played": 1 if new_value else 0,
            "game_count": int(selected_count),
            "players_per_game": PLAYERS_PER_GAME,
        }
    )


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
