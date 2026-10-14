// Mounted in one native agent-preset revision; never starts or owns the Host.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';

const tools = ['ask_user_question', 'bash', 'job_kill', 'job_list', 'job_output', 'skill'];
const budgetKeys = ['max_wall_seconds', 'max_model_requests', 'max_reported_tokens', 'max_output_tokens_per_request'];
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const integer = value => Number.isSafeInteger(value) && value >= 0;
const text = value => typeof value === 'string' && value.length > 0;

function privateDirectory(directory) {
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.uid !== process.getuid() || (stat.mode & 0o077) !== 0
      || fs.realpathSync(directory) !== directory) throw new Error('Shared work ledger directory is not private.');
}

function rejectExistingHardlinks(directory) {
  for (const name of fs.readdirSync(directory)) {
    const entry = path.join(directory, name);
    const stat = fs.lstatSync(entry);
    if (stat.isFile() && stat.nlink > 1) throw new Error('Shared work existing hardlink rejected.');
    if (stat.isDirectory()) rejectExistingHardlinks(entry);
  }
}

function readPrivate(file) {
  const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.uid !== process.getuid() || stat.nlink !== 1 || (stat.mode & 0o077) !== 0) {
      throw new Error('Shared work ledger is not private.');
    }
    const raw = fs.readFileSync(fd, 'utf8');
    return {raw, row: JSON.parse(raw)};
  } finally {
    fs.closeSync(fd);
  }
}

function binding(row) {
  return JSON.stringify({schema: row.schema, service_ref: row.service_ref, host_lease: row.host_lease,
    agent_preset: row.agent_preset, session_id: row.session_id, session_created_at: row.session_created_at,
    generation: row.generation, workspace: row.workspace, provider: row.provider, model: row.model,
    budget: Object.fromEntries(budgetKeys.filter(key => Object.hasOwn(row.budget ?? {}, key)).map(key => [key, row.budget[key]])),
    ...(row.renewWindow ? {renewWindow: Object.fromEntries(Object.entries(row.renewWindow)
      .filter(([key]) => !['started_at_ms', 'stop_reason', 'hook_marker'].includes(key)))} : {})});
}

export default function sharedNativeHook(ctx, config) {
  if (!text(config.ledger_dir) || !path.isAbsolute(config.ledger_dir)
      || !['service_ref', 'host_lease', 'agent_preset'].every(key => text(config[key]))) {
    throw new Error('Shared work Hook requires an exact native preset binding.');
  }
  privateDirectory(config.ledger_dir);
  const hostLease = ctx.get('workflowHostInstance')?.instanceId;
  if (!text(hostLease)) throw new Error('Shared work original public Host identity is unavailable.');
  const admitted = new Map();
  const cleanups = new Set();
  const projection = ctx.get('sessionProjections');
  const jobs = ctx.get('jobs');
  const sourceSnapshot = workspace => {
    const reference = config.source_snapshot;
    if (!reference) return undefined;
    for (const key of ['python', 'reader']) {
      const file = reference[key];
      if (!text(file) || !path.isAbsolute(file) || fs.realpathSync(file) !== file
          || !fs.lstatSync(file).isFile() || file === workspace || file.startsWith(workspace + path.sep)
          || key === 'reader' && (!/^[a-f0-9]{64}$/.test(reference.reader_sha256 ?? '')
            || digest(fs.readFileSync(file)) !== reference.reader_sha256)) {
        throw new Error('Shared work source reader binding changed.');
      }
    }
    const info = fs.statSync(reference.python);
    if (![0, process.getuid()].includes(info.uid) || (info.mode & 0o022) !== 0) throw new Error('Shared work Python permissions are unsafe.');
    fs.accessSync(reference.python, fs.constants.R_OK | fs.constants.X_OK);
    const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(PYTHON|NODE_|DYLD_|LD_|GIT_)/.test(key)));
    const version = spawnSync(reference.python, ['-I', '--version'], {timeout: 5000, maxBuffer: 4096, encoding: 'utf8', shell: false, env: environment});
    const match = (String(version.stdout ?? '') + String(version.stderr ?? '')).trim().match(/^Python (\d+)\.(\d+)\.(\d+)$/);
    if (version.error || version.status !== 0 || !match || Number(match[1]) < 3 || Number(match[1]) === 3 && Number(match[2]) < 11) {
      throw new Error('Shared work Python version is incompatible.');
    }
    const result = spawnSync(reference.python, ['-I', '-B', reference.reader, workspace],
      {timeout: 15000, maxBuffer: 8 * 1024 * 1024, encoding: 'utf8', shell: false,
        env: {...environment, LANG: 'C.UTF-8'}});
    if (result.error || result.status !== 0) throw new Error('Shared work source snapshot is unknown.');
    const value = JSON.parse(result.stdout);
    if (value.head !== null && !/^[a-f0-9]{40}$/.test(value.head)
        || !/^[a-f0-9]{64}$/.test(value.source_digest ?? '') || typeof value.workspace_status !== 'string') {
      throw new Error('Shared work source snapshot is invalid.');
    }
    return {head: value.head, source_digest: value.source_digest, workspace_status: value.workspace_status};
  };
  const ledgerFor = agent => {
    const session = agent.session;
    privateDirectory(config.ledger_dir);
    if (ctx.get('agentPresets').composedPreset(agent.ctx) !== config.agent_preset
        || projection.stateOf(session, 'agentPreset') !== config.agent_preset) {
      throw new Error('Shared work native preset binding changed.');
    }
    const file = path.join(config.ledger_dir, digest(session.id) + '.json');
    const record = readPrivate(file);
    const {row} = record;
    if (row.schema !== 1 || row.session_id !== session.id || row.session_created_at !== session.header.createdAt
        || row.host_lease !== hostLease
        || !['service_ref', 'agent_preset'].every(key => row[key] === config[key])
        || !text(row.generation) || !text(row.workspace) || !path.isAbsolute(row.workspace)
        || fs.realpathSync(row.workspace) !== row.workspace || fs.realpathSync(session.header.cwd) !== row.workspace
        || !text(row.provider) || !text(row.model)
        || !row.budget || typeof row.budget !== 'object' || Array.isArray(row.budget)
        || !Object.keys(row.budget).every(key => budgetKeys.includes(key) && integer(row.budget[key]) && row.budget[key] > 0)) {
      throw new Error('Shared work ledger binding rejected.');
    }
    const policy = ctx.get('sandboxPolicy').resolve({session});
    if (!['read-only', 'workspace-write'].includes(policy.mode)
        || fs.realpathSync(policy.workspaceRoot) !== row.workspace || policy.sessionId !== session.id) {
      throw new Error('Shared work native sandbox binding rejected.');
    }
    const state = row.state;
    const previous = admitted.get(session.id);
    const now = Date.now();
    const fingerprint = digest(binding(row));
    const window = row.renewWindow;
    const windowKeys = window ? Object.keys(window) : [];
    const requiredWindowKeys = ['id', 'message_id', 'host_lease', 'request_base', 'original_state',
      'original_host_lease', 'started_at_ms', 'stop_reason', 'hook_marker'];
    const sealed = window?.sealed_unknown_usage;
    if (window && (windowKeys.some(key => !requiredWindowKeys.includes(key) && key !== 'sealed_unknown_usage')
        || requiredWindowKeys.some(key => !windowKeys.includes(key))
        || sealed !== undefined && (typeof sealed !== 'object' || sealed === null
          || Object.keys(sealed).sort().join() !== ['ordinal', 'event_sha256', 'prior_window_id'].sort().join()
          || !integer(sealed.ordinal) || sealed.ordinal <= 0 || sealed.ordinal >= window.request_base
          || !/^[a-f0-9]{64}$/.test(sealed.event_sha256 ?? '')
          || !text(sealed.prior_window_id) || sealed.prior_window_id === window.id)
        || !text(window.id) || !text(window.message_id) || window.host_lease !== hostLease
        || !text(window.original_host_lease) || window.original_host_lease === hostLease
        || !integer(window.request_base) || window.request_base <= 0 || state.requests < window.request_base
        || window.original_state?.requests !== window.request_base
        || !integer(window.original_state?.started_at_ms) || window.original_state.started_at_ms <= 0
        || !integer(window.original_state?.reported_tokens) || state.reported_tokens < window.original_state.reported_tokens
        || ![null, 'shared-work-wall-budget', 'shared-work-observed-budget'].includes(window.original_state.stop_reason)
        || state.started_at_ms !== window.original_state.started_at_ms || state.stop_reason !== window.original_state.stop_reason
        || !Array.isArray(window.original_state.admissions) || window.original_state.admissions.length !== window.request_base
        || !window.original_state.admissions.every((key, index) => state.admissions[index] === key)
        || window.started_at_ms !== null && (!integer(window.started_at_ms) || window.started_at_ms > now)
        || window.stop_reason !== null && !text(window.stop_reason)
        || ![null, 'same-sid-window-v1'].includes(window.hook_marker)
        || previous?.window && (previous.window.started_at_ms !== window.started_at_ms
          || previous.window.stop_reason !== null && previous.window.stop_reason !== window.stop_reason))) {
      throw new Error('Shared work explicit renewed window changed or is unknown.');
    }
    if (!state || !integer(state.requests) || !integer(state.reported_tokens)
        || !Array.isArray(state.admissions) || state.admissions.length !== state.requests
        || !state.admissions.every(text) || new Set(state.admissions).size !== state.admissions.length
        || state.started_at_ms !== null && !integer(state.started_at_ms)
        || state.started_at_ms > now || state.observed_at_ms !== undefined
          && (!integer(state.observed_at_ms) || state.observed_at_ms > now)
        || state.requests > 0 && state.started_at_ms === null
        || state.stop_reason !== null && !text(state.stop_reason)
        || state.binding_sha256 !== undefined && state.binding_sha256 !== fingerprint
        || previous && (previous.fingerprint !== fingerprint || now < previous.last_wall
          || state.requests < previous.state.requests || state.reported_tokens < previous.state.reported_tokens
          || state.started_at_ms !== previous.state.started_at_ms
          || previous.state.stop_reason !== null && state.stop_reason !== previous.state.stop_reason
          || !previous.state.admissions.every((key, index) => state.admissions[index] === key))) {
      throw new Error('Shared work budget state changed or is unknown.');
    }
    let settlements = 0;
    for (let index = 0; index < session.seq; index++) {
      if (['assistant/message', 'assistant/attempt'].includes(session.eventAt(index).type)) settlements++;
    }
    if (session.header.isSeeded || session.header.parentSession || session.inheritedEventCount !== 0
        || state.requests < settlements) throw new Error('Shared work native journal differs from its budget.');
    if (!previous && state.requests > 0 && (state.binding_sha256 !== fingerprint || state.requests !== settlements)) {
      throw new Error('Shared work request reservation recovery is unknown.');
    }
    if (previous) previous.last_wall = now;
    return {...record, file, fingerprint};
  };
  const save = record => {
    privateDirectory(config.ledger_dir);
    if (readPrivate(record.file).raw !== record.raw) throw new Error('Shared work ledger changed during admission.');
    record.row.state.binding_sha256 = record.fingerprint;
    record.row.state.observed_at_ms = Date.now();
    const temporary = record.file + '.' + crypto.randomUUID();
    try {
      const fd = fs.openSync(temporary, 'wx', 0o600);
      try {
        fs.writeFileSync(fd, JSON.stringify(record.row));
        fs.fsyncSync(fd);
      } finally {fs.closeSync(fd);}
      fs.renameSync(temporary, record.file);
      const directory = fs.openSync(config.ledger_dir, 'r');
      try {fs.fsyncSync(directory);} finally {fs.closeSync(directory);}
      record.raw = JSON.stringify(record.row);
      const previous = admitted.get(record.row.session_id);
      if (previous) {
        previous.state = structuredClone(record.row.state);
        previous.window = record.row.renewWindow ? structuredClone(record.row.renewWindow) : undefined;
      }
    } finally {
      if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
    }
  };
  const usage = (agent, record) => {
    const value = projection.stateOf(agent.session, 'tokenUsage');
    const totals = value?.totals;
    const buckets = ['uncachedInputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'];
    if (!totals || !buckets.every(key => integer(totals[key]))) {
      throw new Error('Shared work reported usage is unavailable.');
    }
    const reported = buckets.reduce((sum, key) => sum + totals[key], 0);
    if (!integer(reported) || reported < record.row.state.reported_tokens) {
      throw new Error('Shared work reported usage regressed.');
    }
    record.row.state.reported_tokens = reported;
    return value;
  };
  const cancel = (agent, reason) => {
    const cause = {kind: 'hook', reason};
    // Abort before clearing: native Session observers cannot append inbox events reentrantly.
    agent.cancel(cause, {keepInbox: true});
    const owned = admitted.get(agent.session.id);
    if (owned?.agent === agent && owned.cleanup) return owned.cleanup;
    const cleanup = Promise.resolve().then(async () => {
      agent.cancel(cause, {keepInbox: false});
      const ids = [];
      for (const job of jobs.list(agent.session.id)) {
        if (job.owner !== agent.session.id || !['running', 'stopping'].includes(job.status)) continue;
        jobs.kill(job.id, agent.session.id, 'Shared work stopped.');
        const terminal = await jobs.wait(job.id, 15000, agent.session.id);
        if (terminal.owner !== agent.session.id || !['completed', 'failed', 'killed'].includes(terminal.status)) {
          throw new Error('Shared work native job stop is unknown.');
        }
        ids.push(job.id);
      }
      const record = ledgerFor(agent);
      record.row.state.stop_cleanup = {kind: 'terminal', job_ids: ids};
      save(record);
    }).catch(error => {
      try {
        const record = ledgerFor(agent);
        record.row.state.stop_cleanup = {kind: 'unknown'};
        save(record);
      } catch {}
      ctx.logger.warn('Shared work cleanup failed: %o', error);
    }).finally(() => cleanups.delete(cleanup));
    cleanups.add(cleanup);
    if (owned?.agent === agent) owned.cleanup = cleanup;
    return cleanup;
  };
  const stop = (agent, record, reason) => {
    const active = record.row.renewWindow ?? record.row.state;
    active.stop_reason ??= reason;
    try {save(record);} finally {
      cancel(agent, active.stop_reason);
    }
    return new Error('Shared work stopped: ' + active.stop_reason);
  };
  const expired = record => record.row.budget.max_wall_seconds !== undefined
    && (record.row.renewWindow ?? record.row.state).started_at_ms !== null
    && (Date.now() - (record.row.renewWindow ?? record.row.state).started_at_ms >= record.row.budget.max_wall_seconds * 1000
      || performance.now() >= (admitted.get(record.row.session_id)?.deadline ?? Infinity));
  const validateSettlements = (session, record) => {
    let settlements = 0;
    let unknownSettlement;
    const sealed = record.row.renewWindow?.sealed_unknown_usage;
    for (let index = 0; index < session.seq; index++) {
      const event = session.eventAt(index);
      if (!['assistant/message', 'assistant/attempt'].includes(event.type)) continue;
      settlements++;
      const value = event.data.usage ?? event.data.stream?.findLast(record => record.type === 'chunk'
        && record.chunk?.type === 'usage')?.chunk.usage;
      const currentWindow = !sealed && record.row.renewWindow?.original_state.stop_reason === 'shared-work-wall-budget'
        && settlements === record.row.renewWindow?.request_base;
      const sealedWindow = sealed && unknownSettlement === undefined && settlements === sealed.ordinal;
      if (value === undefined && event.data.usage === undefined && (currentWindow || sealedWindow)
          && event.type === 'assistant/message' && event.data.interrupted === true
          && integer(event.data.turn) && integer(event.data.step)
          && !event.data.stream?.some(record => record.type === 'chunk' && record.chunk?.type === 'usage')
          && record.row.renewWindow?.original_state.stop_reason === 'shared-work-wall-budget'
          && index + 2 < session.seq) {
        const stepEnd = session.eventAt(index + 1);
        const turnEnd = session.eventAt(index + 2);
        const reason = turnEnd.data?.reason;
        if (stepEnd.type === 'step/end' && stepEnd.data?.turn === event.data.turn && stepEnd.data.step === event.data.step
            && turnEnd.type === 'turn/end' && turnEnd.data?.turn === event.data.turn
            && reason?.kind === 'aborted' && Object.keys(reason).length === 2
            && reason.reason?.kind === 'hook' && reason.reason.reason === 'shared-work-wall-budget'
            && Object.keys(reason.reason).length === 2) {
          if (sealedWindow && digest(JSON.stringify([event, stepEnd, turnEnd])) !== sealed.event_sha256) {
            throw new Error('Shared work native settlement usage is unknown.');
          }
          unknownSettlement = settlements;
          continue; // The sealed old request stays reserved without inventing reported usage.
        }
      }
      if (!value || !['inputTokens', 'outputTokens'].every(key => integer(value[key]))
          || !['cacheReadTokens', 'cacheWriteTokens'].every(key => integer(value[key] ?? 0))) {
        throw new Error('Shared work native settlement usage is unknown.');
      }
    }
  };
  const appendReceipt = (record, suffix, fields) => {
    privateDirectory(config.ledger_dir);
    const file = path.join(config.ledger_dir, digest(record.row.session_id) + suffix + '.jsonl');
    const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_CREAT
      | fs.constants.O_NOFOLLOW, 0o600);
    try {
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || stat.uid !== process.getuid() || stat.nlink !== 1 || (stat.mode & 0o077) !== 0) {
        throw new Error('Shared work receipt is not private.');
      }
      fs.writeSync(fd, JSON.stringify({schema: 1, generation: record.row.generation,
        session_id: record.row.session_id, session_created_at: record.row.session_created_at,
        service_ref: record.row.service_ref, host_lease: record.row.host_lease, ...fields}) + '\n');
      fs.fsyncSync(fd);
    } finally {fs.closeSync(fd);}
  };
  ctx.get('tools').guard(exec => {
    const agent = exec.agent;
    const owned = agent && admitted.get(agent.session.id);
    const reject = reason => {
      if (owned?.agent === agent) (owned.rejected_calls ??= new Set()).add(exec.callId);
      return reason;
    };
    if (!agent || owned?.agent !== agent) return reject('Shared work tool has no admitted owner.');
    try {
      const record = ledgerFor(agent);
      if ((record.row.renewWindow ?? record.row.state).stop_reason || expired(record)) {
        stop(agent, record, expired(record) ? 'shared-work-wall-budget' : 'shared-work-observed-budget');
        return reject('Shared work tool budget stopped.');
      }
      if (!tools.includes(exec.name)) return reject('Shared work tool is outside its verified catalog.');
      if (exec.arguments?.sandbox_permissions === 'danger-full-access') return reject('Shared work danger privilege rejected.');
      if (exec.name === 'bash') rejectExistingHardlinks(record.row.workspace);
      if (['job_kill', 'job_output'].includes(exec.name)
          && jobs.get(exec.arguments?.job_id, agent.session.id).owner !== agent.session.id) {
        return reject('Shared work foreign or unowned job rejected.');
      }
      if (exec.name === 'job_list' && jobs.list(agent.session.id).some(job => job.owner !== agent.session.id)) {
        return reject('Shared work unowned job listing rejected.');
      }
      if (exec.name === 'bash') {
        const before = sourceSnapshot(record.row.workspace);
        const current = ledgerFor(agent);
        if ((current.row.renewWindow ?? current.row.state).stop_reason || expired(current)) {
          stop(agent, current, 'shared-work-wall-budget');
          return reject('Shared work tool budget stopped after source inspection.');
        }
        if (before) (owned.source_before ??= new Map()).set(exec.callId, before);
      }
    } catch {
      return reject('Shared work tool ownership is unknown.');
    }
  });
  ctx.on('tools/result', (exec, result) => {
    const agent = exec.agent;
    const owned = agent && admitted.get(agent.session.id);
    if (owned?.agent !== agent) return;
    try {
      const record = ledgerFor(agent);
      const value = result.value;
      const before = owned.source_before?.get(exec.callId);
      owned.source_before?.delete(exec.callId);
      const after = before ? sourceSnapshot(record.row.workspace) : undefined;
      const output = part => typeof part?.text === 'string' ? {sha256: digest(part.text),
        byte_length: Buffer.byteLength(part.text), truncated: part.truncated} : undefined;
      appendReceipt(record, '.tools', {call_id: exec.callId, name: exec.name, is_error: result.isError,
        native_session_seq: agent.session.seq,
        execution_admission: owned.rejected_calls?.delete(exec.callId) ? 'rejected' : 'admitted',
        ...(typeof exec.arguments?.command === 'string' ? {command_sha256: digest(exec.arguments.command)} : {}),
        ...(before ? {source_before: before, source_after: after} : {}),
        ...(['background', 'promoted'].includes(value?.kind) ? {kind: value.kind, job_id: value.jobId, cwd: value.cwd} : {}),
        ...(value?.kind === 'foreground' ? {kind: value.kind, cwd: value.cwd,
          exit_code: value.exitCode, aborted: value.aborted, timed_out: value.timedOut, sandbox: value.sandbox,
          stdout: output(value.stdout), stderr: output(value.stderr)} : {})});
    } catch (error) {
      cancel(agent, 'shared-work-tool-receipt-failed');
      throw error;
    }
  });
  jobs.events.subscribe({owners: 'scope'}, event => {
    const owned = admitted.get(event.job?.owner);
    if (!owned || !['registered', 'settled', 'removed'].includes(event.type)) return;
    const {agent} = owned;
    try {
      const record = ledgerFor(agent);
      appendReceipt(record, '.jobs', {type: event.type, job_id: event.job.id, owner: event.job.owner,
        kind: event.job.kind, status: event.job.status, started_at_ms: event.job.startedAt,
        finished_at_ms: event.job.finishedAt, command_sha256: digest(event.job.label),
        ...(event.type === 'settled' ? {cause: event.cause, awaited: event.awaited} : {})});
    } catch (error) {
      cancel(agent, 'shared-work-job-receipt-failed');
      throw error;
    }
  });
  ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const agent = context.agent;
    const record = ledgerFor(agent);
    if (!admitted.has(agent.session.id)) {
      rejectExistingHardlinks(record.row.workspace);
      const extra = [...ctx.get('tools').view(agent).restrictableNames].filter(name => !tools.includes(name));
      if (extra.length) agent.ctx.get('tools').restrict({deny: extra});
    }
    // SDK-owned utilities can stay registered; the advertised surface and execution guard remain exact.
    const catalog = ctx.get('tools').schemas(agent).map(schema => schema.name).filter(name => tools.includes(name)).sort();
    if (JSON.stringify(catalog) !== JSON.stringify(tools)) throw new Error('Shared work native tool catalog changed.');
    const assembly = await next();
    const selected = assembly.tools.filter(tool => tools.includes(tool.name));
    if (JSON.stringify(selected.map(tool => tool.name).sort()) !== JSON.stringify(tools)) {
      throw new Error('Shared work assembled native tools changed.');
    }
    return {...assembly, sections: assembly.sections.filter(section => section.name !== 'team:policy'), tools: selected};
  });

  ctx.on('agent/request', async ({agent, turn, step, signal}, next) => {
    signal.throwIfAborted();
    const record = ledgerFor(agent);
    const {state} = record.row;
    validateSettlements(agent.session, record);
    usage(agent, record);
    const active = record.row.renewWindow ?? state;
    if (active.stop_reason || expired(record)) throw stop(agent, record, 'shared-work-wall-budget');
    if (record.row.budget.max_model_requests !== undefined
        && state.requests - (record.row.renewWindow?.request_base ?? 0) >= record.row.budget.max_model_requests) {
      throw stop(agent, record, 'shared-work-observed-budget');
    }
    const catalog = ctx.get('tools').schemas(agent).map(schema => schema.name).filter(name => tools.includes(name)).sort();
    if (JSON.stringify(catalog) !== JSON.stringify(tools)) throw new Error('Shared work native tool catalog changed.');
    const retry = ctx.get('llm').providerRetryPolicy(record.row.provider);
    if (retry?.mode !== 'normal' || retry.maxRetries !== 0) throw new Error('Shared work requires the native no-retry provider policy.');
    const prepared = await ctx.get('llm').prepareCall({provider: record.row.provider,
      model: record.row.model}, signal);
    const window = prepared.context?.contextWindow;
    if (!integer(window) || window <= 0
        || state.model_context_window !== undefined && state.model_context_window !== window) {
      throw new Error('Shared work prepared model bounds changed or are unknown.');
    }
    state.model_context_window = window; // Catalog metadata, not a provider-enforced input-token proof.
    if (!integer(turn) || !integer(step)) throw new Error('Shared work request identity is unavailable.');
    const key = `${turn}:${step}:${agent.session.seq}`;
    if (state.admissions.includes(key)) throw new Error('Shared work request reservation is already consumed.');
    active.started_at_ms ??= Date.now();
    if (record.row.renewWindow) active.hook_marker = 'same-sid-window-v1';
    state.requests++;
    state.admissions.push(key);
    save(record); // A crash or downstream failure never refunds an uncertain request.
    admitted.set(agent.session.id, {agent, fingerprint: record.fingerprint,
      state: structuredClone(state), last_wall: Date.now(),
      ...(record.row.renewWindow ? {window: structuredClone(record.row.renewWindow)} : {}),
      deadline: record.row.budget.max_wall_seconds === undefined ? Infinity
        : performance.now() + record.row.budget.max_wall_seconds * 1000 - (Date.now() - active.started_at_ms)});
    const request = await next();
    const current = ledgerFor(agent);
    signal.throwIfAborted();
    if ((current.row.renewWindow ?? current.row.state).stop_reason || expired(current)) throw stop(agent, current, 'shared-work-wall-budget');
    return {...request, provider: current.row.provider, model: current.row.model};
  });
  ctx.on('session/event', (session, event) => {
    const owned = admitted.get(session.id);
    if (!owned || owned.agent.session !== session) return;
    try {
      const record = ledgerFor(owned.agent);
      const value = usage(owned.agent, record);
      if (['assistant/message', 'assistant/attempt'].includes(event.type)
          && (!event.data.usage && !event.data.stream?.some(record => record.type === 'chunk' && record.chunk?.type === 'usage')
            || value.last?.turn !== event.data.turn || value.last?.step !== event.data.step)) {
        stop(owned.agent, record, 'shared-work-usage-unknown');
      } else save(record);
    } catch (error) {
      cancel(owned.agent, 'shared-work-observer-failed');
      throw error; // The native contained observer retains the original failure.
    }
  });
  ctx.on('agent/disposed', ({agent}) => {
    if (admitted.get(agent.session.id)?.agent === agent) admitted.delete(agent.session.id);
  });
  ctx.effect(() => {
    const timer = setInterval(() => {
      for (const owned of admitted.values()) {
        const {agent} = owned;
        if (owned.cleanup) continue;
        try {
          const record = ledgerFor(agent);
          if ((record.row.renewWindow ?? record.row.state).stop_reason || expired(record)) stop(agent, record, 'shared-work-wall-budget');
        } catch {
          cancel(agent, 'shared-work-ownership-unknown');
        }
      }
    }, 250);
    return async () => {
      clearInterval(timer);
      for (const {agent} of admitted.values()) {
        cancel(agent, 'shared-work-hook-unloaded');
      }
      await Promise.all([...cleanups]);
      admitted.clear();
    };
  });
}
sharedNativeHook.inject = ['sessionProjections', 'tools', 'llm', 'agentPresets', 'sandboxPolicy', 'jobs', 'workflowHostInstance'];
