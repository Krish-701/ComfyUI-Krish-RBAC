import jwt
from aiohttp import web
from datetime import datetime, timedelta, timezone

from .users_db import UsersDB
from .access_control import AccessControl
from .logger import Logger
from .session_store import issue_session, validate_session, clear_session


class JWTAuth:
    def __init__(
        self,
        users_db: UsersDB,
        access_control: AccessControl,
        logger: Logger,
        secret_key: str,
        expire_minutes: int = 12 * 60,
        algorithm: str = "HS256",
    ):
        self.users_db = users_db
        self.access_control = access_control
        self.logger = logger

        self.expire_minutes = expire_minutes
        self.algorithm = algorithm

        self.__secret_key = secret_key

    @staticmethod
    def get_token_from_request(request: web.Request) -> str:
        """Extract token from request headers or cookies."""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[len("Bearer ") :]
        return request.cookies.get("jwt_token")

    def create_access_token(
        self,
        data: dict,
        expire_minutes=None,
        *,
        single_session: bool = True,
    ) -> str:
        """
        Create a JWT access token.

        When single_session=True (default for web login), a new session id is
        issued and any previous web login for that user stops working.
        Set single_session=False for long-lived API tokens that should not
        kick off interactive sessions (or pass token_type=\"api\").
        """
        to_encode = data.copy()
        if not expire_minutes:
            expire_minutes = self.expire_minutes
        expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
        to_encode.update({"exp": expire})

        token_type = str(to_encode.get("token_type") or "session").lower()
        uid = to_encode.get("id")
        if single_session and token_type != "api" and uid:
            sid = issue_session(str(uid))
            to_encode["sid"] = sid
            to_encode["token_type"] = "session"
        elif "token_type" not in to_encode:
            to_encode["token_type"] = token_type

        return jwt.encode(to_encode, self.__secret_key, algorithm=self.algorithm)

    def decode_access_token(self, token: str) -> dict:
        """Decode a JWT access token."""
        return jwt.decode(token, self.__secret_key, algorithms=[self.algorithm])

    def invalidate_user_session(self, user_id: str | None) -> None:
        clear_session(user_id)

    def create_jwt_middleware(
        self,
        public: tuple = (),
        public_prefixes: tuple = (),
        public_suffixes: tuple = (),
    ) -> web.middleware:
        """Create middleware for JWT authentication."""

        @web.middleware
        async def jwt_middleware(request: web.Request, handler) -> web.Response:
            """Middleware to handle JWT authentication."""
            if (
                request.path in public
                or request.path.startswith(public_prefixes)
                or request.path.endswith(public_suffixes)
            ):
                return await handler(request)

            token = self.get_token_from_request(request)

            if not token:
                return await handle_unauthorized_access(request, "/login")

            try:
                user = self.decode_access_token(token)
                user_id = user.get("id")
                username = user.get("username")
                db_uid, rec = self.users_db.get_user(username=username)
                if not user_id == db_uid:
                    raise ValueError(
                        f"User with username: {username} is not in the database"
                    )
                if rec and rec.get("disabled"):
                    return await handle_unauthorized_access(
                        request, "/logout", message="Account disabled"
                    )

                # Single-session: web tokens must match the latest login
                token_type = str(user.get("token_type") or "session").lower()
                if token_type != "api":
                    sid = user.get("sid")
                    if not validate_session(user_id, sid):
                        return await handle_unauthorized_access(
                            request,
                            "/logout",
                            message="Session ended — you signed in elsewhere",
                            code="SESSION_REPLACED",
                        )

                # Force password change: only allow limited endpoints
                if rec and rec.get("must_change_password"):
                    path = request.path or ""
                    allowed = (
                        path in (
                            "/logout",
                            "/usgromana/api/change-password",
                            "/usgromana/api/me",
                            "/change_password",
                        )
                        or path.startswith("/usgromana/css")
                        or path.startswith("/usgromana/js")
                        or path.startswith("/usgromana/assets")
                    )
                    if not allowed:
                        accept = request.headers.get("Accept", "")
                        if "text/html" in accept:
                            return web.HTTPFound("/change_password")
                        return web.json_response(
                            {
                                "error": "Password change required",
                                "code": "MUST_CHANGE_PASSWORD",
                            },
                            status=403,
                        )

                request["user_id"] = user_id
                request["user"] = username

                # Prefer username for storage folders (output/<username>/).
                # Still set UUID as fallback key for legacy paths.
                storage_key = username or user_id
                # set_fallback on prompt so worker + /view after prompt share context;
                # also set on /view so image loads resolve the correct user folder.
                path = request.path or ""
                set_fallback = (
                    path in ("/api/prompt", "/prompt", "/view")
                    or path.startswith("/api/prompt")
                    or path.startswith("/api/view")
                    or path.startswith("/api/assets")
                    or path.startswith("/api/history")
                    or path == "/history"
                    # Previews use type=temp — keep user context for temp chroot
                    or "filename=" in (request.query_string or "")
                )
                self.access_control.set_current_user_id(storage_key, set_fallback)
                # Keep UUID available on request for APIs that need it
                request["user_id"] = user_id
                request["user"] = username
                try:
                    from .presence import touch
                    touch(username)
                except Exception:
                    pass

            except jwt.ExpiredSignatureError:
                return await handle_unauthorized_access(
                    request, "/logout", message="Token has expired"
                )
            except jwt.DecodeError:
                return await handle_unauthorized_access(
                    request, "/logout", message="Token is invalid"
                )
            except Exception as e:
                self.logger.error(f"Unexpected error during token decoding: {e}")
                return await handle_unauthorized_access(
                    request, "/logout", message="Unexpected error"
                )

            return await handler(request)

        async def handle_unauthorized_access(
            request: web.Request,
            redirect_path: str,
            message: str = "Authentication required",
            code: str | None = None,
        ) -> web.Response:
            """Handle unauthorized access cases."""
            accept_header = request.headers.get("Accept", "")
            if "text/html" in accept_header:
                return web.HTTPFound(redirect_path)
            else:
                body = {"error": message}
                if code:
                    body["code"] = code
                return web.json_response(body, status=401)

        return jwt_middleware
