// Tests for the launcher, with Node's own test runner and no dependencies: `npm test`.
// The launcher is started as a separate process with a PATH that holds only a stand-in for
// python3, so these tests do not depend on the Python of the machine. The last test runs the
// real thing once, in a temporary queue directory, when a python3 is there.
'use strict';

const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const LAUNCHER = path.resolve(__dirname, '..', 'bin', 'owner-queue.js');
const { which, findPython, noPython, ROOT, ENTRY } = require(LAUNCHER);
const PACKAGE = require(path.join(ROOT, 'package.json'));

// A stand-in for python3, run by this Node so that it behaves the same wherever the tests run
// (a shell script cannot catch a signal that was ignored when it started). The launcher's
// version check (-c) is answered with STUB_CHECK. After that, STUB_MODE says what it does:
//   record  write its arguments as JSON to STUB_OUT and exit with STUB_EXIT
//   echo    copy standard input to standard output
//   wait    write "ready" to STUB_OUT, then on a signal write its name there and exit 42
//   die     end itself with SIGTERM
const STUB = `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
if (args[0] === '-c') process.exit(Number(process.env.STUB_CHECK || 0));
const mode = process.env.STUB_MODE || 'record';
if (mode === 'record') {
  fs.writeFileSync(process.env.STUB_OUT, JSON.stringify(args));
  process.exit(Number(process.env.STUB_EXIT || 0));
} else if (mode === 'echo') {
  process.stdin.pipe(process.stdout);
} else if (mode === 'wait') {
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(signal, () => {
      fs.appendFileSync(process.env.STUB_OUT, signal);
      process.exit(42);
    });
  }
  fs.writeFileSync(process.env.STUB_OUT, 'ready ');
  setInterval(() => {}, 1000);
} else if (mode === 'die') {
  process.kill(process.pid, 'SIGTERM');
  setInterval(() => {}, 1000);
}
`;

function sandbox(t, names = ['python3']) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'oq-npm-')));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin);
  for (const name of names) fs.writeFileSync(path.join(bin, name), STUB, { mode: 0o755 });
  const out = path.join(dir, 'out');
  return { dir, bin, out, env: { PATH: bin, HOME: dir, STUB_OUT: out } };
}

function run(args, env, input) {
  return spawnSync(process.execPath, [LAUNCHER, ...args], { env, input, timeout: 20000 });
}

async function until(check, what) {
  for (let i = 0; i < 200; i++) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.fail(`timed out waiting for ${what}`);
}

test('which finds a program on absolute PATH entries only', (t) => {
  const box = sandbox(t);
  assert.equal(which('python3', { PATH: box.bin }), path.join(box.bin, 'python3'));
  assert.equal(which('python3', { PATH: `:.:bin:${box.bin}` }), path.join(box.bin, 'python3'));
  assert.equal(which('python3', { PATH: '.:bin:' }), null);
  assert.equal(which('nothing-like-this', { PATH: box.bin }), null);
  fs.chmodSync(path.join(box.bin, 'python3'), 0o644);
  assert.equal(which('python3', { PATH: box.bin }), null, 'a file that is not executable does not count');
});

test('arguments reach Python unchanged, after the bundled entry point', (t) => {
  const box = sandbox(t);
  const args = ['add', 'two words', '--url=https://example.com/?a=1&b=2', '$(echo no)', ';', '*'];
  const done = run(args, box.env);
  assert.equal(done.status, 0);
  assert.equal(done.stderr.toString(), '');
  assert.deepEqual(JSON.parse(fs.readFileSync(box.out, 'utf8')), [ENTRY, ...args]);
  assert.ok(fs.statSync(ENTRY).isFile(), 'the entry point is in the package');
});

test('the exit code of Python is the exit code of the launcher', (t) => {
  const box = sandbox(t);
  for (const code of [0, 1, 2, 7]) {
    assert.equal(run(['count'], { ...box.env, STUB_EXIT: String(code) }).status, code);
  }
});

test('standard input and output pass through', (t) => {
  const box = sandbox(t);
  const input = Buffer.from('{"hook_event_name":"Stop"}\n\u00e9\u0000\n'.repeat(2000));
  const done = run(['hook'], { ...box.env, STUB_MODE: 'echo' }, input);
  assert.equal(done.status, 0);
  assert.ok(done.stdout.equals(input));
});

test('python is used when there is no python3', (t) => {
  const box = sandbox(t, ['python']);
  assert.equal(run(['count'], box.env).status, 0);
  assert.equal(findPython({ PATH: box.bin }).python, path.join(box.bin, 'python'));
});

test('no Python: one line on standard error, exit code 127, nothing started', (t) => {
  const box = sandbox(t, []);
  const done = run(['next'], box.env);
  assert.equal(done.status, 127);
  assert.equal(done.stdout.toString(), '');
  assert.match(done.stderr.toString(), /^owner-queue: needs Python 3\.9 or later, and no python3 was found on PATH\. .*\n$/);
  assert.equal(done.stderr.toString().trim().split('\n').length, 1);
  assert.equal(fs.existsSync(box.out), false);
});

test('a Python that is too old is named, and not used', (t) => {
  const box = sandbox(t);
  const done = run(['next'], { ...box.env, STUB_CHECK: '3' });
  assert.equal(done.status, 127);
  assert.match(done.stderr.toString(), /is older/);
  assert.ok(done.stderr.toString().includes(path.join(box.bin, 'python3')));
  assert.equal(done.stderr.toString().trim().split('\n').length, 1);
  assert.equal(fs.existsSync(box.out), false);
  assert.match(noPython(null), /no python3 was found/);
});

test('SIGTERM, SIGINT and SIGHUP are passed on to Python', async (t) => {
  for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
    const box = sandbox(t);
    const child = spawn(process.execPath, [LAUNCHER, 'next'], { env: { ...box.env, STUB_MODE: 'wait' }, stdio: 'ignore' });
    const ended = new Promise((resolve) => child.on('exit', (code, sig) => resolve({ code, sig })));
    await until(() => fs.existsSync(box.out) && fs.readFileSync(box.out, 'utf8') === 'ready ', 'the stand-in to start');
    child.kill(signal);
    const { code, sig } = await ended;
    assert.equal(fs.readFileSync(box.out, 'utf8'), `ready ${signal}`, 'Python got the same signal');
    assert.deepEqual({ code, sig }, { code: 42, sig: null }, 'and its exit code is ours');
  }
});

test('when Python is ended by a signal, so is the launcher', async (t) => {
  const box = sandbox(t);
  const child = spawn(process.execPath, [LAUNCHER, 'next'], { env: { ...box.env, STUB_MODE: 'die' }, stdio: 'ignore' });
  const { code, sig } = await new Promise((resolve) => child.on('exit', (c, s) => resolve({ code: c, sig: s })));
  assert.equal(sig, 'SIGTERM');
  assert.equal(code, null);
});

test('package.json: this launcher is the command, nothing runs at install time', () => {
  assert.deepEqual(PACKAGE.bin, { 'owner-queue': 'packaging/npm/bin/owner-queue.js' });
  assert.equal(path.resolve(ROOT, PACKAGE.bin['owner-queue']), LAUNCHER);
  assert.ok(fs.statSync(LAUNCHER).mode & 0o111, 'the launcher is executable');
  for (const name of ['preinstall', 'install', 'postinstall', 'prepare']) {
    assert.equal(name in (PACKAGE.scripts || {}), false, name);
  }
  assert.equal(PACKAGE.dependencies, undefined);
});

test('with a real Python: the version is the one in package.json, and the queue works', (t) => {
  const python = findPython().python;
  if (!python) return t.skip('no Python 3.9 or later on this machine');
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'oq-npm-')));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const env = { PATH: path.dirname(python), HOME: dir, OWNER_QUEUE_DIR: path.join(dir, 'queue'), OWNER_QUEUE_NTFY: 'off' };
  assert.equal(run(['--version'], env).stdout.toString(), `owner-queue ${PACKAGE.version}\n`);
  assert.equal(run(['count'], env).stdout.toString(), '0\n');
  assert.match(run(['add', 'a note'], env).stdout.toString(), /^added [0-9a-f]+\n$/);
  assert.equal(run(['count'], env).stdout.toString(), '1\n');
  assert.equal(run(['hook'], env, '{}').status, 0);
  assert.equal(run(['frobnicate'], env).status, 2);
});
