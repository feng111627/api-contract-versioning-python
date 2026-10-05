"""演示用的内存数据存储（非持久化，仅服务于版本兼容示例）。"""

from __future__ import annotations

from threading import Lock

#: 固定演示数据：各版本读到的是同一份数据，差异只体现在契约层。
USERS: dict[int, dict[str, object]] = {
    1: {"id": 1, "name": "Alice Wang", "email": "alice@example.com", "tier": "pro"},
    2: {"id": 2, "name": "Bob Li", "email": "bob@example.com", "tier": "free"},
}

_LOCK = Lock()
_NEXT_ID = 3


def list_users() -> list[dict[str, object]]:
    return [dict(row) for row in USERS.values()]


def get_user(user_id: int) -> dict[str, object] | None:
    row = USERS.get(user_id)
    return dict(row) if row else None


def create_user(*, name: str, email: str | None, tier: str = "free") -> dict[str, object]:
    global _NEXT_ID
    with _LOCK:
        user_id = _NEXT_ID
        _NEXT_ID += 1
        row: dict[str, object] = {
            "id": user_id,
            "name": name,
            "email": email,
            "tier": tier,
        }
        USERS[user_id] = row
        return dict(row)


def reset_for_tests() -> None:
    """重置内存数据到初始演示集（仅供测试使用）。"""
    global _NEXT_ID
    USERS.clear()
    USERS.update(
        {
            1: {"id": 1, "name": "Alice Wang", "email": "alice@example.com", "tier": "pro"},
            2: {"id": 2, "name": "Bob Li", "email": "bob@example.com", "tier": "free"},
        }
    )
    _NEXT_ID = 3
