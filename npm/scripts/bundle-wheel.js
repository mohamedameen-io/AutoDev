import {
  existsSync,
  mkdirSync,
  copyFileSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ROOT = join(__dirname, "..");
const DIST_DIR = join(ROOT, "dist");
const WHEEL_DIR = join(ROOT, "wheel");
const PACKAGE_JSON = join(ROOT, "package.json");
// Sibling python source — `src/_version.py` lives one level above `npm/`.
const PYTHON_VERSION_FILE = join(ROOT, "..", "src", "_version.py");

if (!existsSync(WHEEL_DIR)) {
  mkdirSync(WHEEL_DIR, { recursive: true });
}

if (!existsSync(DIST_DIR)) {
  mkdirSync(DIST_DIR, { recursive: true });
}

const distFiles = ["cli/index.js", "cli/index.d.ts", "index.js", "index.d.ts"];
for (const file of distFiles) {
  const src = join(DIST_DIR, file);
  if (existsSync(src)) {
    console.log(`Bundle includes: ${file}`);
  }
}

console.log(`\nWheel directory: ${WHEEL_DIR}`);
console.log(
  "To bundle the Python wheel, run from the repo root:\n" +
    "  pip wheel . --wheel-dir npm/wheel --no-deps\n" +
    "  # or with uv:\n" +
    "  uv pip wheel . --python/path /path/to/python --dest npm/wheel\n"
);

const wheels = readdirSync(WHEEL_DIR).filter((f) => f.endsWith(".whl"));
if (wheels.length > 0) {
  console.log(`Found wheel(s): ${wheels.join(", ")}`);
} else {
  console.log("No wheel found in wheel/ directory.");
}

// Sync npm/package.json version with src/_version.py so each Python
// release publishes to npm at the matching version. The publish
// workflow's `npm publish` would otherwise fail with "cannot publish
// over the previously published versions: <stale>". (Hardened after
// the v0.22.0 → v0.24.0 release-series npm-publish flop.)
function syncVersionFromPython() {
  if (!existsSync(PYTHON_VERSION_FILE)) {
    console.log(
      `(version sync skipped: ${PYTHON_VERSION_FILE} not found)`,
    );
    return;
  }
  const text = readFileSync(PYTHON_VERSION_FILE, "utf8");
  const match = text.match(/__version__\s*=\s*["']([^"']+)["']/);
  if (!match) {
    console.log(
      "(version sync skipped: __version__ pattern not found in _version.py)",
    );
    return;
  }
  const pyVersion = match[1];
  const pkg = JSON.parse(readFileSync(PACKAGE_JSON, "utf8"));
  if (pkg.version === pyVersion) {
    console.log(`npm package.json version already synced: ${pyVersion}`);
    return;
  }
  console.log(
    `Syncing npm package.json version: ${pkg.version} -> ${pyVersion}`,
  );
  pkg.version = pyVersion;
  writeFileSync(PACKAGE_JSON, JSON.stringify(pkg, null, 2) + "\n", "utf8");
}

syncVersionFromPython();
