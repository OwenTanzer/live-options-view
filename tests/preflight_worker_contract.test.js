// Contract fixture for the provisioning probe: the real Worker's non-mutating
// response to an empty JSON body with a supplied operator header.
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const { handleRegister } = await import(pathToFileURL(path.join(__dirname, '..', 'backend', 'auth.js')));
  const key = 'operator-key';
  const request = (headers) => new Request('https://options.moopertonic.net/api/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: '{}',
  });
  const writes = [];
  const env = {
    BOT_REGISTRATION_KEY: key,
    USERS: { get: async () => null, put: async (...args) => writes.push(args) },
  };

  const accepted = await handleRegister(request({ 'X-Bot-Registration-Key': key }), env);
  assert.equal(accepted.status, 400);
  const acceptedBody = await accepted.json();
  assert.deepEqual(acceptedBody, {
    error: 'Username must be 3-40 characters: letters, numbers, underscore',
  });

  const rejected = await handleRegister(request({ 'X-Bot-Registration-Key': 'wrong' }), env);
  assert.equal(rejected.status, 403);
  assert.deepEqual(await rejected.json(), { error: 'Invalid bot registration key' });

  const missing = await handleRegister(request({ 'X-Bot-Registration-Key': key }), { ...env, BOT_REGISTRATION_KEY: '' });
  assert.equal(missing.status, 403);
  assert.deepEqual(await missing.json(), { error: 'Invalid bot registration key' });

  assert.equal(writes.length, 0, 'validation responses must not create or write an account');
  console.log('4 worker contract checks passed.');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
