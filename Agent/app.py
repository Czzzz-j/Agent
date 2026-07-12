from __future__ import annotations

import json
import os
import uuid

import requests
import streamlit as st

from db.mysql_client import MySQLClient


API_BASE = os.getenv("API_BASE", "http://localhost:8008")
API_KEY = os.getenv("API_KEY", "")

_mysql_pool = None


def _get_mysql_pool():
    global _mysql_pool
    if _mysql_pool is None:
        _mysql_pool = MySQLClient.get_pool()
    return _mysql_pool


def get_or_create_user(user_uuid: str | None = None) -> str:
    """Create a demo user if the current session does not have one."""
    if user_uuid is None:
        user_uuid = st.session_state.get("user_uuid")
        if user_uuid:
            return user_uuid

    new_uuid = str(uuid.uuid4())
    conn = _get_mysql_pool().get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (uuid) VALUES (%s)", (new_uuid,))
        conn.commit()
    except Exception as exc:
        print(f"Failed to create user: {exc}")
    finally:
        cursor.close()
        conn.close()

    st.session_state["user_uuid"] = new_uuid
    return new_uuid


def save_dislike(question: str, answer: str) -> None:
    user_uuid = st.session_state.get("user_uuid", "anonymous")
    session_id = st.session_state.get("session_id", "")
    conn = _get_mysql_pool().get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO feedbacks (user_uuid, session_id, user_question, assistant_answer)
            VALUES (%s, %s, %s, %s)
            """,
            (user_uuid, session_id, question, answer),
        )
        conn.commit()
        st.toast("已记录反馈，我们会继续改进。")
    except Exception as exc:
        st.error(f"反馈记录失败: {exc}")
    finally:
        cursor.close()
        conn.close()


st.set_page_config(page_title="全屋家具智能管家", page_icon="🏠", layout="centered")

st.markdown(
    """
<style>
    .stApp { background-color: #f7f9fc; }
    .main > div { background-color: #fff; border-radius: 20px; padding: 1rem 1.5rem; }
    .stButton > button { border-radius: 30px; background-color: #4a6fa5; color: white; }
    [data-testid="stSidebar"] { background-color: #f0f2f6; }
    footer { visibility: hidden; }
</style>
""",
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "user_uuid" not in st.session_state:
    get_or_create_user()
if "session_id" not in st.session_state:
    st.session_state["session_id"] = str(uuid.uuid4())

col1, col2 = st.columns([1, 5])
with col1:
    st.markdown('<p style="font-size:50px;">🏠</p>', unsafe_allow_html=True)
with col2:
    st.title("全屋家具智能管家")
    st.caption("沙发 / 床 / 餐桌 / 衣柜 / 地毯 / 灯具 / 电视柜 / 梳妆台 / 电脑桌 / 浴室柜 / 橱柜")

for idx, msg in enumerate(st.session_state["messages"]):
    with st.chat_message(msg["role"]):
        st.write(msg["content"])
        if msg["role"] == "assistant":
            user_q = ""
            for i in range(idx - 1, -1, -1):
                if st.session_state["messages"][i]["role"] == "user":
                    user_q = st.session_state["messages"][i]["content"]
                    break
            if st.button("👎", key=f"dislike_{idx}"):
                save_dislike(user_q, msg["content"])
                st.rerun()

prompt = st.chat_input("请输入问题...")


def stream_generator(prompt: str, session_id: str, user_uuid: str, request_id: str, error_holder: list[str]):
    payload = {
        "message": prompt,
        "session_id": session_id,
        "user_uuid": user_uuid,
        "request_id": request_id,
    }
    headers = {"X-API-Key": API_KEY}
    try:
        response = requests.post(f"{API_BASE}/chat", json=payload, headers=headers, stream=True, timeout=120)
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            data = line.decode("utf-8")
            if not data.startswith("data: "):
                continue
            payload_data = json.loads(data[6:])
            if "content" in payload_data:
                yield payload_data["content"]
            elif payload_data.get("done"):
                break
            elif "error" in payload_data:
                error_holder.append(payload_data["error"])
                break
    except Exception as exc:
        error_holder.append(f"请求失败: {exc}")


if prompt:
    st.chat_message("user").write(prompt)
    st.session_state["messages"].append({"role": "user", "content": prompt})

    session_id = st.session_state.get("session_id", "")
    user_uuid = st.session_state.get("user_uuid", "")
    request_id = str(uuid.uuid4())
    stream_errors: list[str] = []

    full_response = st.write_stream(
        stream_generator(prompt, session_id, user_uuid, request_id, stream_errors)
    )
    if full_response:
        st.session_state["messages"].append({"role": "assistant", "content": full_response})
    if stream_errors:
        st.error(stream_errors[-1])
    st.rerun()
