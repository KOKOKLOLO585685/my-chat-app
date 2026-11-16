import os
import shutil
import sqlite3
import hashlib
import uvicorn
import socketio
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

os.makedirs("avatars", exist_ok=True)

def hash_pass(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def init_db():
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        avatar TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS friends (
        user_id INTEGER,
        friend_id INTEGER,
        status TEXT,
        PRIMARY KEY (user_id, friend_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        admin_id INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY (group_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        sender_id INTEGER,
        sender_name TEXT,
        text TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/avatars", StaticFiles(directory="avatars"), name="avatars")

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, app)

class AuthData(BaseModel):
    username: str
    password: str

class FriendReqData(BaseModel):
    sender_id: int
    target_username: str

class AcceptFriendData(BaseModel):
    user_id: int
    requester_id: int

class GroupData(BaseModel):
    name: str
    admin_id: int
    member_ids: list[int]

class JoinGroupData(BaseModel):
    group_id: int
    user_id: int

@app.post("/register")
def register(data: AuthData):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password, avatar) VALUES (?, ?, ?)",
                  (data.username, hash_pass(data.password), ""))
        conn.commit()
        user_id = c.lastrowid
        return {"id": user_id, "username": data.username, "avatar": ""}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="اسم المستخدم مسجل مسبقاً")
    finally:
        conn.close()

@app.post("/login")
def login(data: AuthData):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("SELECT id, username, password, avatar FROM users WHERE username = ?", (data.username,))
    user = c.fetchone()
    conn.close()
    if not user or user != hash_pass(data.password):
        raise HTTPException(status_code=400, detail="بيانات الدخول غير صحيحة")
    return {"id": user, "username": user, "avatar": user}

@app.post("/upload-avatar/{user_id}")
def upload_avatar(user_id: int, file: UploadFile = File(...)):
    file_path = f"avatars/user_{user_id}_{file.filename}"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    avatar_url = f"/{file_path}"
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("UPDATE users SET avatar = ? WHERE id = ?", (avatar_url, user_id))
    conn.commit()
    conn.close()
    return {"avatar": avatar_url}

@app.post("/friend-request")
async def send_friend_request(data: FriendReqData):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE username = ?", (data.target_username,))
    target = c.fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    target_id = target
    if target_id == data.sender_id:
        conn.close()
        raise HTTPException(status_code=400, detail="لا يمكنك إضافة نفسك")
    c.execute("INSERT OR IGNORE INTO friends (user_id, friend_id, status) VALUES (?, ?, 'pending')",
              (data.sender_id, target_id))
    conn.commit()
    await sio.emit("refresh_home", {"msg": "طلب صداقة جديد!"}, room=f"user_{target_id}")
    conn.close()
    return {"message": "تم إرسال الطلب"}

@app.post("/accept-friend")
async def accept_friend(data: AcceptFriendData):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("UPDATE friends SET status = 'accepted' WHERE user_id = ? AND friend_id = ?",
              (data.requester_id, data.user_id))
    c.execute("INSERT OR REPLACE INTO friends (user_id, friend_id, status) VALUES (?, ?, 'accepted')",
              (data.user_id, data.requester_id))
    conn.commit()
    conn.close()
    await sio.emit("refresh_home", {"msg": "تم قبول طلب الصداقة!"}, room=f"user_{data.requester_id}")
    await sio.emit("refresh_home", {"msg": "تم قبول طلب الصداقة!"}, room=f"user_{data.user_id}")
    return {"message": "أصبحتما أصدقاء"}

@app.post("/create-group")
async def create_group(data: GroupData):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("INSERT INTO groups (name, admin_id) VALUES (?, ?)", (data.name, data.admin_id))
    group_id = c.lastrowid
    all_members = set([data.admin_id] + data.member_ids)
    for m_id in all_members:
        c.execute("INSERT INTO group_members (group_id, user_id) VALUES (?, ?)", (group_id, m_id))
        await sio.emit("refresh_home", {"msg": f"تمت إضافتك لقروب {data.name}"}, room=f"user_{m_id}")
    conn.commit()
    conn.close()
    return {"id": group_id, "name": data.name}

@app.get("/search-groups")
def search_groups(q: str = ""):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("SELECT id, name FROM groups WHERE name LIKE ?", (f"%{q}%",))
    results = [{"id": r, "name": r} for r in c.fetchall()]
    conn.close()
    return results

@app.post("/join-group")
async def join_group(data: JoinGroupData):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)", (data.group_id, data.user_id))
    conn.commit()
    conn.close()
    await sio.emit("refresh_home", {"msg": "تم الانضمام للقروب بنجاح!"}, room=f"user_{data.user_id}")
    return {"message": "تم الانضمام"}

@app.get("/user-data/{user_id}")
def get_user_data(user_id: int):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("SELECT id, username, avatar FROM users WHERE id = ?", (user_id,))
    u = c.fetchone()

    c.execute('''SELECT u.id, u.username, u.avatar FROM users u
                 JOIN friends f ON u.id = f.friend_id
                 WHERE f.user_id = ? AND f.status = 'accepted' AND u.id != ?''', (user_id, user_id))
    friends = [{"id": r, "username": r, "avatar": r} for r in c.fetchall()]

    c.execute('''SELECT u.id, u.username, u.avatar FROM users u
                 JOIN friends f ON u.id = f.friend_id
                 WHERE f.friend_id = ? AND f.status = 'pending' AND u.id != ?''', (user_id, user_id))
    requests = [{"id": r, "username": r, "avatar": r} for r in c.fetchall()]

    c.execute('''SELECT g.id, g.name FROM groups g
                 JOIN group_members gm ON g.id = gm.group_id
                 WHERE gm.user_id = ?''', (user_id,))
    groups = [{"id": r, "name": r} for r in c.fetchall()]
    conn.close()

    return {
        "user": {"id": u, "username": u, "avatar": u},
        "friends": friends,
        "requests": requests,
        "groups": groups
    }

@app.get("/messages/{chat_id}")
def get_messages(chat_id: str):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("SELECT sender_id, sender_name, text FROM messages WHERE chat_id = ?", (chat_id,))
    msgs = [{"sender_id": r, "sender_name": r, "text": r} for r in c.fetchall()]
    conn.close()
    return msgs

@sio.event
async def register_user_socket(sid, user_id):
    await sio.enter_room(sid, f"user_{user_id}")

@sio.event
async def join_chat(sid, chat_id):
    await sio.enter_room(sid, chat_id)

@sio.event
async def send_message(sid, data):
    conn = sqlite3.connect("chat.db")
    c = conn.cursor()
    c.execute("INSERT INTO messages (chat_id, sender_id, sender_name, text) VALUES (?, ?, ?, ?)",
              (data["chat_id"], data["sender_id"], data["sender_name"], data["text"]))
    conn.commit()
    conn.close()
    await sio.emit("receive_message", data, room=data["chat_id"])

if __name__ == "__main__":
    uvicorn.run(socket_app, host="0.0.0.0", port=8000)
