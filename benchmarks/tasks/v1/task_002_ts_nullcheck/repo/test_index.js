const assert = require("assert");
const { getUserById } = require("./index.js");

// Existing user.
const found = getUserById(1);
assert.strictEqual(found.ok, true, "id=1 should return ok=true");
assert.strictEqual(found.user.id, 1);
assert.strictEqual(found.user.name, "Alice");

// Missing user — must NOT throw.
let missing;
try {
  missing = getUserById(999);
} catch (err) {
  assert.fail(`getUserById(999) threw: ${err.message}`);
}
assert.strictEqual(missing.ok, false, "id=999 should return ok=false");
assert.strictEqual(missing.status, 404, "id=999 should have status=404");
assert.ok(typeof missing.message === "string" && missing.message.length > 0);

console.log("ok");
