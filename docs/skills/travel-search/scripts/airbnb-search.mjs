#!/usr/bin/env node
import { spawn } from 'child_process';

const [,, location, checkin, checkout, adultsStr] = process.argv;
const adults = parseInt(adultsStr || '2');
const serverPath = process.env.HOME + '/.local/lib/node_modules/@openbnb/mcp-server-airbnb';

const server = spawn(process.execPath, [serverPath], {
  stdio: ['pipe', 'pipe', 'pipe']
});

let buf = '';
let id = 1;
let result = null;
let resolveResult;

const resultPromise = new Promise((resolve, reject) => {
  resolveResult = resolve;
  setTimeout(() => reject(new Error('Timeout after 25s')), 25000);
});

function send(method, params = {}) {
  const msg = JSON.stringify({ jsonrpc: '2.0', id: id++, method, params }) + '\n';
  server.stdin.write(msg);
}

server.stdout.on('data', (chunk) => {
  buf += chunk.toString();
  const lines = buf.split('\n');
  buf = lines.pop();
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed.id === id - 1 || (parsed.result && parsed.result.content)) {
        result = parsed;
        resolveResult(parsed);
      }
    } catch {}
  }
});

server.on('error', (err) => {
  console.error('Server error:', err.message);
});

// Initialize
send('initialize', {
  protocolVersion: '2025-06-18',
  capabilities: {},
  clientInfo: { name: 'openclaw', version: '1.0' }
});

// Wait a moment then send initialized notification
setTimeout(() => {
  send('notifications/initialized', {});
  
  // Small delay then search
  setTimeout(() => {
    send('tools/call', {
      name: 'airbnb_search',
      arguments: { location, checkin, checkout, adults }
    });
  }, 500);
}, 500);

try {
  const data = await resultPromise;
  console.log(JSON.stringify(data));
} catch (e) {
  console.error('Error:', e.message);
  process.exit(1);
} finally {
  server.kill();
  process.exit(0);
}
