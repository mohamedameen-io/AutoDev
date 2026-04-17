# Project Intent: JWT Authentication

## Goal

Build JWT-based authentication for a Python web service (FastAPI). Users should be able to register, log in, and access protected endpoints using Bearer tokens.

## Constraints

- Python 3.11+
- FastAPI + Pydantic v2
- `python-jose` for JWT encoding/decoding
- `passlib[bcrypt]` for password hashing
- Tokens expire after 30 minutes (access) and 7 days (refresh)
- No database required — use an in-memory user store for this iteration
- All endpoints must have OpenAPI documentation

## Non-goals

- OAuth2 social login (Google, GitHub, etc.)
- Email verification
- Password reset flow
- Persistent storage (database integration is a future phase)

## Success criteria

- `POST /auth/register` creates a user and returns a token pair
- `POST /auth/login` validates credentials and returns a token pair
- `POST /auth/refresh` exchanges a valid refresh token for a new access token
- `GET /users/me` returns the current user's profile (requires valid access token)
- Expired or invalid tokens return HTTP 401
- Passwords are never stored in plaintext
- At least 80% test coverage on auth logic
- `pytest` suite passes with no failures

## Expected Plan Structure

AutoDev should produce a plan with approximately these tasks:

1. **Models** — `User`, `Token`, `TokenPair` pydantic models; in-memory user store
2. **JWT utilities** — `create_access_token`, `create_refresh_token`, `decode_token` functions
3. **Auth routes** — `/auth/register`, `/auth/login`, `/auth/refresh` endpoints
4. **Dependency** — `get_current_user` FastAPI dependency for protected routes
5. **Protected routes** — `GET /users/me` using the dependency
6. **Tests** — pytest suite covering happy path + error cases for all endpoints
