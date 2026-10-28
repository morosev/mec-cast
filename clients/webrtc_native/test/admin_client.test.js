// The Node admin client against the protocol fixture every implementation
// reads (services/admin/tests/vectors.json): it must understand every frame
// the admin sends, and say hello in the shape the admin expects. No socket:
// frames go in through onFrame and come out of a recording send().
//
//   npm test        (or: node --test test/)

const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");

const { AdminClient } = require("../admin_client");

const vectors = JSON.parse(
  fs.readFileSync(path.join(__dirname, "../../../services/admin/tests/vectors.json"), "utf-8"),
);

function client(statusFields = {}) {
  const sent = [];
  const calls = [];
  const c = new AdminClient({
    url: "ws://unused",
    nodeType: "sender",
    handlers: {
      onStart: (runId, args) => calls.push(["start", runId, args]),
      onStop: () => {
        calls.push(["stop"]);
        return { webrtc_rows: 3 };
      },
      status: () => ({ state: "idle", run_id: null, params: {}, ...statusFields }),
    },
    log: () => {},
  });
  c.send = (type, payload) => sent.push({ type, payload });
  return { c, sent, calls };
}

const frame = (name) => Buffer.from(JSON.stringify(vectors[name]));

test("a welcome with an active run starts it, with the run's params", () => {
  const { c, calls } = client();
  c.onFrame(frame("welcome"));
  const run = vectors.welcome.payload.active_run;
  assert.deepStrictEqual(calls, [["start", run.run_id, run.params]]);
});

test("a welcome without one starts nothing", () => {
  const { c, calls } = client();
  c.onFrame(frame("welcome_no_active_run"));
  assert.deepStrictEqual(calls, []);
});

test("run.start starts, and is acknowledged, then reported", () => {
  const { c, sent, calls } = client();
  c.onFrame(frame("command_run_start"));
  const cmd = vectors.command_run_start;
  assert.deepStrictEqual(calls, [["start", cmd.payload.run_id, cmd.payload.args]]);
  assert.deepStrictEqual(sent.map((f) => f.type), ["ack", "status"]);
  assert.strictEqual(sent[0].payload.in_reply_to, cmd.msg_id);
  assert.strictEqual(sent[0].payload.ok, true);
});

test("run.stop stops, and its status carries the report", () => {
  const { c, sent, calls } = client();
  c.onFrame(frame("command_run_stop"));
  assert.deepStrictEqual(calls, [["stop"]]);
  const status = sent.find((f) => f.type === "status");
  assert.deepStrictEqual(status.payload.report, { webrtc_rows: 3 });
  assert.strictEqual(status.payload.node_type, "sender");
});

test("a ping is answered with a pong", () => {
  const { c, sent } = client();
  c.onFrame(frame("ping"));
  assert.deepStrictEqual(sent.map((f) => f.type), ["pong"]);
});

test("a command meant for another node type is refused, not obeyed", () => {
  const { c, sent, calls } = client();
  c.onFrame(frame("command_ran_policy"));
  assert.deepStrictEqual(calls, []);
  assert.strictEqual(sent[0].type, "ack");
  assert.strictEqual(sent[0].payload.ok, false);
});

test("a frame on another protocol version is ignored", () => {
  const { c, sent, calls } = client();
  c.onFrame(Buffer.from(JSON.stringify({ ...vectors.command_run_start, v: 2 })));
  assert.deepStrictEqual([sent, calls], [[], []]);
});

test("hello has the fixture's shape", () => {
  const expected = vectors.hello_sender.payload;
  const { c } = client({ params: expected.params });
  const hello = c.helloPayload();
  assert.deepStrictEqual(Object.keys(hello).sort(), Object.keys(expected).sort());
  assert.strictEqual(hello.node_type, "sender");
  assert.match(hello.node_id, /^sender-[^-.]+(-[^-.]+)*-0$/);
  assert.strictEqual(hello.autostart, true);
});
