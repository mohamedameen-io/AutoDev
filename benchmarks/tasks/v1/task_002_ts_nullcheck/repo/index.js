// A user record is returned for id=1; null otherwise.
function getUserFromDb(id) {
  if (id === 1) return { id: 1, name: "Alice" };
  return null;
}

// Bug: dereferences `user` even when getUserFromDb returns null.
function getUserById(id) {
  const user = getUserFromDb(id);
  return { ok: true, user: { id: user.id, name: user.name } };
}

module.exports = { getUserById };
