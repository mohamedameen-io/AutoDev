# task_002_ts_nullcheck — handle DB-returned null

`getUserById()` in `index.js` crashes with `TypeError: Cannot read properties
of null (reading 'id')` if the DB returns `null`. Add a null check and return
a 404-style error object instead.

## Expected behaviour

```
getUserById(1)    -> { ok: true,  user: { id: 1, name: "Alice" } }
getUserById(999)  -> { ok: false, status: 404, message: <some string> }
```

## How to verify

Run `bash test_command.sh` from this repo root. It must exit `0`.
