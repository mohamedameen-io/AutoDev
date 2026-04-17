import { existsSync, mkdirSync, copyFileSync, readdirSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ROOT = join(__dirname, "..");
const DIST_DIR = join(ROOT, "dist");
const WHEEL_DIR = join(ROOT, "wheel");

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
