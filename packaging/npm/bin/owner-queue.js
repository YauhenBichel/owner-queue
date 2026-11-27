#!/usr/bin/env node
// The npm package owner-queue carries the Python source of the tool. This launcher finds a
// Python 3.9 or later and runs that source with it, then gets out of the way:
//   - the child inherits stdin, stdout and stderr (hooks read their JSON from stdin);
//   - arguments go as an argv array, never through a shell;
//   - anything the launcher itself says goes to stderr, in one line;
//   - SIGINT, SIGTERM and SIGHUP are passed on, and the child's exit code is ours.
// No dependencies, no install script, nothing downloaded at install time or at first run.
'use strict';

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const ENTRY = path.join(ROOT, 'bin', 'owner-queue');
const NEEDS = [3, 9];
const CHECK = `import sys; sys.exit(0 if sys.version_info >= (${NEEDS[0]}, ${NEEDS[1]}) else 3)`;

// The full path of `name` on PATH, or null. Empty and relative PATH entries are skipped:
// they mean "whatever folder I am in", and a project folder must not supply our Python.
function which(name, env = process.env) {
  for (const dir of String(env.PATH || '').split(path.delimiter)) {
    if (!dir || !path.isAbsolute(dir)) continue;
    const file = path.join(dir, name);
    try {
      if (!fs.statSync(file).isFile()) continue;
      fs.accessSync(file, fs.constants.X_OK);
      return file;
    } catch {
      // not here; keep looking
    }
  }
  return null;
}

// { python } for the first Python on PATH that is new enough, else { python: null, old }.
function findPython(env = process.env) {
  let old = null;
  for (const name of ['python3', 'python']) {
    const python = which(name, env);
    if (!python) continue;
    const check = spawnSync(python, ['-c', CHECK], { stdio: 'ignore', shell: false, timeout: 20000 });
    if (check.status === 0) return { python, old: null };
    old = old || python;
  }
  return { python: null, old };
}

function noPython(old) {
  const need = `Python ${NEEDS.join('.')} or later`;
  return old
    ? `owner-queue: needs ${need}, and ${old} is older (or does not run). Install a newer python3 and run this again.`
    : `owner-queue: needs ${need}, and no python3 was found on PATH. Install Python and run this again.`;
}

function main() {
  const say = (line) => process.stderr.write(line.endsWith('\n') ? line : line + '\n');
  if (process.platform === 'win32') {
    say('owner-queue: Windows is not supported (the queue uses POSIX file locks).');
    process.exitCode = 1;
    return;
  }
  const { python, old } = findPython();
  if (!python) {
    say(noPython(old));
    process.exitCode = 127;
    return;
  }

  const child = spawn(python, [ENTRY, ...process.argv.slice(2)], { stdio: 'inherit', shell: false });

  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
  const forward = {};
  for (const signal of signals) {
    forward[signal] = () => {
      try {
        child.kill(signal);
      } catch {
        // the child is already gone; its exit event ends us
      }
    };
    process.on(signal, forward[signal]);
  }

  child.on('error', (error) => {
    say(`owner-queue: could not start ${python}: ${error.message}`);
    process.exit(error.code === 'ENOENT' ? 127 : 126);
  });

  child.on('exit', (code, signal) => {
    if (signal) {
      // End the way the child ended, so whoever started us sees the same thing.
      for (const name of signals) process.removeListener(name, forward[name]);
      try {
        process.kill(process.pid, signal);
      } catch {
        // a signal this platform cannot send: fall through to the exit code
      }
      const number = os.constants.signals[signal] || 0;
      setTimeout(() => process.exit(128 + number), 500);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

module.exports = { which, findPython, noPython, ROOT, ENTRY };

if (require.main === module) main();
