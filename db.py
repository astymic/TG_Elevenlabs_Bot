import sqlite3
from config import DB_PATH

def init_db():
    """Initializes the database and creates the table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        conn.commit()

def is_user_authorized(user_id: int) -> bool:
    """Checks if a user is authorized."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM authorized_users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def add_user(user_id: int):
    """Adds a user to the list of authorized users."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # INSERT OR IGNORE will not raise an error if the user already exists
        cursor.execute("INSERT OR IGNORE INTO authorized_users (user_id) VALUES (?)", (user_id,))
        conn.commit()

def remove_user(user_id: int):
    """Removes a user from the authorized list (logout)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM authorized_users WHERE user_id = ?", (user_id,))
        conn.commit()