# JWT Auth Example

This example demonstrates AutoDev on a realistic multi-task spec: building JWT-based authentication for a FastAPI service.

## What AutoDev will build

Given the spec in `spec.md`, AutoDev should produce a plan with 5–6 tasks and implement:

```
jwt_auth/
├── models.py          # User, Token, TokenPair pydantic models + in-memory store
├── jwt_utils.py       # create_access_token, create_refresh_token, decode_token
├── routes/
│   ├── __init__.py
│   ├── auth.py        # /auth/register, /auth/login, /auth/refresh
│   └── users.py       # GET /users/me (protected)
├── dependencies.py    # get_current_user FastAPI dependency
├── main.py            # FastAPI app assembly
└── tests/
    ├── test_models.py
    ├── test_jwt_utils.py
    └── test_routes.py
```

## How to run

```bash
# From this directory
 autodev init
 autodev plan "$(cat spec.md)"
 autodev execute
```

## Expected plan output

After `autodev plan`, you should see a table like:

```
Phase  Task    Title                    Files
1      1.1     User models + store      models.py
1      1.2     JWT utilities            jwt_utils.py
2      2.1     Auth routes              routes/auth.py
2      2.2     User dependency          dependencies.py
2      2.3     Protected routes         routes/users.py, main.py
3      3.1     Test suite               tests/
```

The exact task breakdown may differ — the architect and plan tournament will refine the structure based on the spec.

## Tournament behavior

This example is a good candidate for the plan tournament because:
- The spec has multiple interacting components (models, JWT, routes, dependencies)
- The architect's initial draft often misses the refresh token flow or the dependency injection pattern
- The tournament's critic reliably catches these gaps

With default settings (3 judges, max 15 rounds), the plan tournament typically converges in 3–5 rounds for this spec.

## Sample tournament output

```
Tournament passes
Pass  Winner  Scores              Valid judges  Elapsed (s)
1     B       A=1, AB=2, B=3      3             12.4
2     AB      A=2, AB=4, B=0      3             11.8
3     AB      A=3, AB=3, B=0      3             10.2   ← tie → incumbent wins
Tournament complete. passes=3 final_winner=AB
```

In this example, the tournament converged after 3 passes with the synthesized version (AB) as the winner — a common outcome when the architect's draft (A) and the revision (B) each have complementary strengths.
