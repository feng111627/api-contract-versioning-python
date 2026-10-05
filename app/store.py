"""示例业务的内存存储与业务异常。"""
from __future__ import annotations

from dataclasses import dataclass

from app.errors import BusinessError, ErrorCode


class UserNotFoundError(BusinessError):
    code = ErrorCode.NOT_FOUND
    http_status = 404

    def __init__(self, user_id: int) -> None:
        super().__init__(f"user {user_id} not found", [{"field": "id", "value": user_id}])


class DuplicateEmailError(BusinessError):
    http_status = 409

    def __init__(self, email: str) -> None:
        super().__init__(f"email already registered: {email}", [{"field": "email", "value": email}])


@dataclass
class UserRecord:
    id: int
    name: str
    display_name: str | None
    nickname: str | None
    email: str | None


class UserStore:
    """进程内用户存储；reset() 供测试隔离。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._users: dict[int, UserRecord] = {}
        self._next_id = 1

    def create(
        self,
        name: str,
        display_name: str | None = None,
        nickname: str | None = None,
        email: str | None = None,
    ) -> UserRecord:
        if email and any(u.email == email for u in self._users.values()):
            raise DuplicateEmailError(email)
        record = UserRecord(
            id=self._next_id,
            name=name,
            display_name=display_name,
            nickname=nickname,
            email=email,
        )
        self._users[record.id] = record
        self._next_id += 1
        return record

    def get(self, user_id: int) -> UserRecord:
        record = self._users.get(user_id)
        if record is None:
            raise UserNotFoundError(user_id)
        return record


store = UserStore()
