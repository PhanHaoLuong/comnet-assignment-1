import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from common.constants import (
    HASH_SIZE,
    MAX_PASSWORD_BYTES,
    SALT_SIZE,
    SCRYPT_MAXMEM,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    USERNAME_PATTERN,
    ACCOUNTS_FILE,
)

logger = logging.getLogger(__name__)

# Used to burn the same scrypt time for unknown users (see authenticate()).
_DUMMY_SALT = bytes(SALT_SIZE)
_DUMMY_HASH = bytes(HASH_SIZE)


# =================== usernames ==============================================

# canonical form (lowercase ASCII) reutrns True
def is_valid_username(username: object) -> bool:
    return (
        isinstance(username, str) and username.isascii() and USERNAME_PATTERN.fullmatch(username) is not None
    )

# Strip + lowercase wire input
def normalize_username(username: object) -> Optional[str]:
    if not isinstance(username, str) or not username.isascii():
        # isascii() first: some non-ASCII chars (e.g. the Kelvin sign) lowercase into ASCII letters and would otherwise sneak through.
        return None
    name = username.strip().lower()
    return name if is_valid_username(name) else None


# =================== passwords ==============================================

def is_valid_password(password: object) -> bool:
    if not isinstance(password, str) or not password:
        return False
    try:
        return len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES
    except UnicodeEncodeError:      # lone surrogates can arrive via JSON
        return False


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=HASH_SIZE,
    )


def hash_password(password: str) -> tuple[bytes, bytes]:
    if not is_valid_password(password):
        raise ValueError("invalid password")
    salt = os.urandom(SALT_SIZE)
    return salt, _scrypt(password, salt)


def verify_password(password: object, salt: object, expected_hash: object) -> bool:
    if not is_valid_password(password):
        return False
    if not isinstance(salt, bytes) or len(salt) != SALT_SIZE:
        return False
    if not isinstance(expected_hash, bytes) or len(expected_hash) != HASH_SIZE:
        return False
    return hmac.compare_digest(_scrypt(password, salt), expected_hash)


# =================== account records ========================================

def create_account_record(username: str, password: str) -> dict:
    if not is_valid_username(username):
        raise ValueError("invalid username")
    salt, password_hash = hash_password(password)
    return {
        "username": username,
        "algorithm": "scrypt",
        "scrypt_n": SCRYPT_N,
        "scrypt_r": SCRYPT_R,
        "scrypt_p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "password_hash": base64.b64encode(password_hash).decode("ascii"),
    }


def add_account(accounts: dict, username: str, password: str) -> str:
    name = normalize_username(username)
    if name is None:
        raise ValueError("invalid username")
    if name in accounts:
        raise ValueError(f"username '{name}' already exists")
    accounts[name] = create_account_record(name, password)
    return name


def set_password(accounts: dict, username: str, password: str) -> str:
    name = normalize_username(username)
    if name is None or name not in accounts:
        raise ValueError("Account not found")
    accounts[name] = create_account_record(name, password)
    return name


def remove_account(accounts: dict, username: str) -> str:
    name = normalize_username(username)
    if name is None or name not in accounts:
        raise ValueError("Account not found")
    del accounts[name]
    return name


# =================== storage ================================================

def save_accounts(accounts: dict, file_path: str) -> None:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file with mode 0600, so the hashes stay private.
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(accounts, file, indent=4)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

# if invalid return {}
def load_accounts(file_path: str) -> dict:
    path = Path(file_path)
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        accounts = json.load(file)

    if not isinstance(accounts, dict):
        raise ValueError("account file must contain a JSON object")

    for key, record in accounts.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise ValueError("incorrect account record")

        username = record.get("username")
        if not is_valid_username(username):
            raise ValueError("invalid username in account file")
        if key != username:
            raise ValueError("account key does not match username")

        if record.get("algorithm") != "scrypt":
            raise ValueError("unsupported password hashing algorithm")
        if (
            record.get("scrypt_n") != SCRYPT_N
            or record.get("scrypt_r") != SCRYPT_R
            or record.get("scrypt_p") != SCRYPT_P
        ):
            raise ValueError("unsupported scrypt parameters")

        try:
            salt = base64.b64decode(record["salt"], validate=True)
            password_hash = base64.b64decode(record["password_hash"], validate=True)
        except (KeyError, ValueError, TypeError):
            raise ValueError("invalid encoded password data")
        if len(salt) != SALT_SIZE or len(password_hash) != HASH_SIZE:
            raise ValueError("invalid salt or password hash length")

    return accounts


class AccountStore:

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime_ns = self._stat()
        self.accounts: dict = load_accounts(str(self.path))

    def _stat(self) -> Optional[int]:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

# Reload if the file changed. Returns True if accounts were replaced
    def refresh(self) -> bool:
        with self._lock:
            mtime = self._stat()
            if mtime == self._mtime_ns:
                return False
            try:
                self.accounts = load_accounts(str(self.path))
            except (OSError, ValueError):
                logger.warning("could not reload %s; keeping old accounts",
                               self.path, exc_info=True)
                return False
            self._mtime_ns = mtime
            return True


# =================== authentication =========================================

# Blocking check against an accounts dict
def check_credentials(accounts: dict, username: object, password: object) -> bool:
    if not is_valid_password(password):
        return False
    record = accounts.get(username) if is_valid_username(username) else None

    if record is None:
        verify_password(password, _DUMMY_SALT, _DUMMY_HASH)   # burn the time
        return False
    try:
        salt = base64.b64decode(record["salt"], validate=True)
        expected = base64.b64decode(record["password_hash"], validate=True)
    except (KeyError, ValueError, TypeError):
        return False
    return verify_password(password, salt, expected)

_store: Optional[AccountStore] = None
_store_lock = threading.Lock()

# Load the accounts file. Call once at tracker startup
def configure(path: str = ACCOUNTS_FILE) -> AccountStore:
    global _store
    store = AccountStore(path)
    with _store_lock:
        _store = store
    return store


def _get_store() -> AccountStore:
    with _store_lock:
        store = _store
    return store if store is not None else configure()


def _authenticate_blocking(username: object, password: object) -> bool:
    store = _get_store()
    store.refresh()                      # pick up accounts the admin just added
    return check_credentials(store.accounts, username, password)


async def authenticate(username: str, password: str) -> bool:
    return await asyncio.to_thread(_authenticate_blocking, username, password)