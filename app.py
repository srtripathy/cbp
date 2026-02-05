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


def build_engine():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
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
    weeks = get_weeks()

    return render_template(
        "week.html",
        club_name=CLUB_NAME,
        week=week,
        weeks=weeks,
        players=players,
        games=range(1, GAMES_PER_WEEK + 1),
        played_map=played_map,
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
        conn.execute(
            update(week_player_games_table)
            .where(
                (week_player_games_table.c.week_id == week_id)
                & (week_player_games_table.c.player_id == player_id)
                & (week_player_games_table.c.game_no == game_no)
            )
            .values(played=new_value)
        )

    return jsonify({"ok": True, "played": 1 if new_value else 0})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
