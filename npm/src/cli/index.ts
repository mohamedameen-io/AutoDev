#!/usr/bin/env node

import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { existsSync, readFileSync, mkdirSync } from "fs";
import { execFileSync } from "child_process";
import { homedir } from "os";
import which from "which";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const NPM_ROOT = join(__dirname, "..", "..");
const CONFIG_DIR = join(homedir(), ".config", "autodev");
const VENV_DIR = join(CONFIG_DIR, "venv");
const WHEEL_DIR = join(NPM_ROOT, "wheel");
const PYTHON_BIN = join(VENV_DIR, "bin", "python");
const AUTODEV_BIN = join(VENV_DIR, "bin", "autodev");

function ensureConfigDir(): void {
  if (!existsSync(CONFIG_DIR)) {
    mkdirSync(CONFIG_DIR, { recursive: true });
  }
}

function ensureVenv(): void {
  if (!existsSync(VENV_DIR)) {
    console.log("Setting up Python environment...");
    try {
      which.sync("python3");
    } catch {
      console.error("Python 3 not found. Please install Python 3.11+.");
      process.exit(1);
    }
    execFileSync("python3", ["-m", "venv", VENV_DIR], { stdio: "inherit" });
    installWheel();
  }
}

function installWheel(): void {
  const wheels = [
    join(WHEEL_DIR, "ai_autodev-0.1.1-py3-none-any.whl"),
    join(WHEEL_DIR, "ai_autodev-py3-none-any.whl"),
  ];

  let wheelPath: string | null = null;
  for (const wheel of wheels) {
    if (existsSync(wheel)) {
      wheelPath = wheel;
      break;
    }
  }

  if (!wheelPath) {
    console.error(
      `No wheel found in ${WHEEL_DIR}. Run 'npm run build' in the npm/ directory first.`
    );
    process.exit(1);
  }

  console.log(`Installing wheel: ${wheelPath}`);
  execFileSync(PYTHON_BIN, ["-m", "pip", "install", "--upgrade", "pip"], {
    stdio: "inherit",
  });
  execFileSync(PYTHON_BIN, ["-m", "pip", "install", wheelPath], {
    stdio: "inherit",
  });
}

function getVersion(): string {
  const pkg = JSON.parse(readFileSync(join(NPM_ROOT, "package.json"), "utf-8"));
  return pkg.version;
}

function main(): void {
  const args = process.argv.slice(2);
  const command = args[0];

  if (command === "install") {
    ensureConfigDir();
    ensureVenv();
    console.log("AutoDev installed successfully!");
    return;
  }

  if (command === "uninstall") {
    if (existsSync(VENV_DIR)) {
      const { rmSync } = require("child_process");
      rmSync(VENV_DIR, { recursive: true, force: true });
      console.log("AutoDev uninstalled.");
    }
    if (existsSync(CONFIG_DIR)) {
      const { rmSync } = require("child_process");
      rmSync(CONFIG_DIR, { recursive: true, force: true });
    }
    return;
  }

  if (command === "version" || command === "--version" || command === "-v") {
    console.log(`autodev ${getVersion()}`);
    return;
  }

  if (command === "doctor") {
    ensureVenv();
    try {
      execFileSync(AUTODEV_BIN, ["doctor"], { stdio: "inherit" });
    } catch (e) {
      process.exit(1);
    }
    return;
  }

  ensureVenv();
  try {
    execFileSync(AUTODEV_BIN, args, { stdio: "inherit" });
  } catch (e: unknown) {
    if (e && typeof e === "object" && "status" in e) {
      process.exit((e as { status: number }).status);
    }
    process.exit(1);
  }
}

main();
