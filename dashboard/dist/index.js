(function () {
  'use strict';
  const sdk = window.__HERMES_PLUGIN_SDK__;
  const React = sdk.React;
  const h = React.createElement;
  const api = '/api/plugins/ghost-hermes-pm';
  const blank = { projectId: '', name: '', repoPath: '', artifacts: '', profileId: '', nativeProfile: '',
    identityRef: '', role: 'project_lead', capability: 'development', parent: '', bot: '', credential: '', codex: '' };

  function Projects() {
    const [snapshot, setSnapshot] = React.useState(null);
    const [error, setError] = React.useState('');
    const [form, setForm] = React.useState(blank);
    const [review, setReview] = React.useState(null);
    const [saving, setSaving] = React.useState(false);
    const [controlTexts, setControlTexts] = React.useState({});
    const [controlIntents, setControlIntents] = React.useState({});
    const [humanAnswers, setHumanAnswers] = React.useState({});
    const [humanReviewed, setHumanReviewed] = React.useState({});
    const [humanIntents, setHumanIntents] = React.useState({});
    const [preparationPlans, setPreparationPlans] = React.useState({});
    const [manualTargets, setManualTargets] = React.useState({});
    const [manualGrantIntents, setManualGrantIntents] = React.useState({});
    const [roleForm, setRoleForm] = React.useState({ sender: '', target: '', issue: '', anchor: '', channels: '' });
    const [roleReview, setRoleReview] = React.useState(null);
    const [observerForm, setObserverForm] = React.useState({ id: '', kind: 'daemon', projects: '', adapter_ref: '' });
    async function refresh() {
      try { const value = await sdk.fetchJSON(api + '/snapshot'); setSnapshot(value); setError(''); return value; }
      catch (e) { setError(String(e.message || e)); }
    }
    React.useEffect(function () { refresh(); }, []);
    function field(key, label, options) {
      const props = { value: form[key], onChange: function (e) {
        setForm(Object.assign({}, form, { [key]: e.target.value })); setReview(null);
      }, style: { width: '100%', padding: '7px', border: '1px solid #8886', borderRadius: '5px',
                  color: 'inherit', background: 'transparent' } };
      return h('label', { key: key, style: { display: 'block', fontSize: '13px' } }, label,
        options ? h('select', props, options.map(function (o) { return h('option', { key: o[0], value: o[0] }, o[1]); }))
                : h('input', props));
    }
    function preview(e) {
      e.preventDefault();
      const refs = {};
      ['bot', 'credential', 'codex'].forEach(function (key) { if (form[key]) refs[key] = form[key]; });
      const change = {};
      if (form.projectId) change.project = { id: form.projectId, name: form.name, repo_path: form.repoPath,
        test_artifact_paths: form.artifacts.split('\n').map(function (p) { return p.trim(); }).filter(Boolean) };
      if (form.profileId) change.profile = { id: form.profileId, native_profile: form.nativeProfile,
        identity_ref: form.identityRef, role: form.role, capability: form.capability,
        project_id: ['steward', 'independent'].includes(form.role) ? null : form.projectId,
        parent_profile_id: form.role === 'subproject_lead' ? form.parent || null : null, connection_refs: refs };
      setReview({ expected_version: snapshot.version, change: change });
    }
    async function apply() {
      setSaving(true);
      try { await sdk.fetchJSON(api + '/directory', { method: 'POST', body: JSON.stringify(review) });
        setReview(null); await refresh(); }
      catch (e) { setError(String(e.message || e)); }
      finally { setSaving(false); }
    }
    function manualField(record, key, label) {
      const selected = manualTargets[record.id] || {};
      return h('label', { style: { display: 'block' } }, label, h('input', { value: selected[key] || '',
        onChange: function (e) { setManualTargets(Object.assign({}, manualTargets, { [record.id]: Object.assign({}, selected, { [key]: e.target.value }) })); },
        style: { margin: '6px', padding: '7px', color: 'inherit', background: 'transparent', border: '1px solid #8886' } }));
    }
    async function manualGrantAction(action, record) {
      const selected = manualTargets[record.id] || {};
      const previousGrant = (snapshot.control_grants || []).find(function (g) { return g.id === record.control_grant_id; });
      const savedIntent = previousGrant && previousGrant.status === 'returned' ? null : manualGrantIntents[record.id];
      const body = action === 'return' ? { action: 'return', request_id: record.id, grant_id: record.control_grant_id } :
        savedIntent || { action: 'takeover', request_id: record.id, grant_id: window.crypto.randomUUID(),
          manual_session_id: String(selected.manual_session_id || '').trim(), expected_turn_id: String(selected.turn_id || '').trim() };
      if (action === 'takeover') setManualGrantIntents(Object.assign({}, manualGrantIntents, { [record.id]: body }));
      setSaving(true);
      try { await sdk.fetchJSON(api + '/task', { method: 'POST', body: JSON.stringify(body) }); await refresh(); }
      catch (e) { await refresh(); setError(String(e.message || e) + ' · 授权 ID：' + body.grant_id); }
      finally { setSaving(false); }
    }
    function roleField(key, label, multiline) {
      return h('label', { style: { display: 'block', margin: '8px 0' } }, label,
        h(multiline ? 'textarea' : 'input', { value: roleForm[key], rows: multiline ? 4 : undefined,
          onChange: function (e) { setRoleForm(Object.assign({}, roleForm, { [key]: e.target.value })); setRoleReview(null); },
          style: { width: '100%', padding: '7px', color: 'inherit', background: 'transparent', border: '1px solid #8886' } }));
    }
    function previewRole(action) {
      try {
        const details = action === 'register_channels' ? { channels: JSON.parse(roleForm.channels) } : {
          sender_profile_id: roleForm.sender.trim(), target_profile_id: roleForm.target.trim(),
          issue_url: roleForm.issue.trim(), source_anchor: JSON.parse(roleForm.anchor) };
        setRoleReview({ action: action, details: details }); setError('');
      } catch (e) { setError('请填写可核对的群路由或原本人消息锚 JSON。'); }
    }
    async function applyRole() {
      setSaving(true);
      try { await sdk.fetchJSON(api + '/collaboration', { method: 'POST', body: JSON.stringify(roleReview) }); setRoleReview(null); await refresh(); }
      catch (e) { await refresh(); setError(String(e.message || e)); }
      finally { setSaving(false); }
    }
    async function observationAction(register) {
      const body = register ? { action: 'register', registration: { id: observerForm.id.trim(), kind: observerForm.kind,
        adapter_ref: observerForm.adapter_ref.trim(), project_ids: observerForm.projects.split(',').map(function (id) { return id.trim(); }).filter(Boolean) } } : { action: 'refresh' };
      setSaving(true);
      try { await sdk.fetchJSON(api + '/observations', { method: 'POST', body: JSON.stringify(body) }); await refresh(); }
      catch (e) { await refresh(); setError(String(e.message || e)); }
      finally { setSaving(false); }
    }
    function observerField(key, label, options) {
      const props = { value: observerForm[key], onChange: function (e) { setObserverForm(Object.assign({}, observerForm, { [key]: e.target.value })); },
        style: { margin: '6px', padding: '7px', color: 'inherit', background: 'transparent', border: '1px solid #8886' } };
      return h('label', { style: { display: 'block' } }, label, options ? h('select', props, options.map(function (kind) { return h('option', { key: kind, value: kind }, kind); })) : h('input', props));
    }
    async function taskAction(action, requestId) {
      setSaving(true);
      try { await sdk.fetchJSON(api + '/task', { method: 'POST', body: JSON.stringify({ action: action, request_id: requestId }) }); await refresh(); }
      catch (e) { await refresh(); setError(String(e.message || e)); }
      finally { setSaving(false); }
    }
    async function answerHuman(record, question, decision) {
      if (humanIntents[question.id]) {
        await refresh(); setError('先核对原请求状态；保留人工答复 ID：' + humanIntents[question.id].reply_id); return;
      }
      let response;
      if (question.category === 'approval') {
        response = { decision: decision, operation_id: question.operation_id, scope: 'turn' };
        if (question.method === 'item/permissions/requestApproval') {
          try { response.permissions = decision === 'decline' ? {} : JSON.parse(humanAnswers[question.id + ':permissions'] || ''); }
          catch (e) { setError('请填写本次明确批准的权限 JSON 子集。'); return; }
        }
      } else {
        const answers = {};
        question.questions.forEach(function (q) { answers[q.id] = [humanAnswers[question.id + ':' + q.id] || '']; });
        response = { answers: answers };
      }
      const body = { action: 'answer', request_id: record.id, human_request_id: question.id,
        reply_id: window.crypto.randomUUID(), response: response };
      setHumanIntents(Object.assign({}, humanIntents, { [question.id]: body }));
      setSaving(true);
      try {
        await sdk.fetchJSON(api + '/task', { method: 'POST', body: JSON.stringify(body) });
        await refresh();
      } catch (e) {
        await refresh(); setError(String(e.message || e) + ' · 先核对请求；答复 ID：' + body.reply_id);
      } finally { setSaving(false); }
    }
    async function prepareAction(record) {
      const selected = preparationPlans[record.id] || {};
      const plan = { branch: String(selected.branch || '').trim(), commit: selected.commit === 'unborn' ? null : String(selected.commit || '').trim(),
        dependencies: String(selected.dependencies || '').split(',').map(function (id) { return id.trim(); }).filter(Boolean),
        issue_updated_at: record.accepted_scope.updated_at };
      if (selected.workspace_digest) plan.workspace_digest = selected.workspace_digest.trim();
      setSaving(true);
      try { await sdk.fetchJSON(api + '/task', { method: 'POST', body: JSON.stringify({ action: 'prepare', request_id: record.id, plan: plan }) }); await refresh(); }
      catch (e) { await refresh(); setError(String(e.message || e)); }
      finally { setSaving(false); }
    }
    function preparationField(record, key, label) {
      const selected = preparationPlans[record.id] || {};
      return h('label', { style: { display: 'block' } }, label, h('input', { value: selected[key] || '',
        onChange: function (e) { setPreparationPlans(Object.assign({}, preparationPlans, { [record.id]: Object.assign({}, selected, { [key]: e.target.value }) })); },
        style: { margin: '6px', padding: '7px', color: 'inherit', background: 'transparent', border: '1px solid #8886' } }));
    }
    async function controlAction(action, record) {
      const key = record.id + ':' + action;
      const body = controlIntents[key] || { action: action, request_id: record.id,
        instruction_id: window.crypto.randomUUID(), expected_turn_id: record.session.turn_id,
        text: action === 'stop' ? null : controlTexts[record.id] || '' };
      setControlIntents(Object.assign({}, controlIntents, { [key]: body }));
      setSaving(true);
      try {
        const result = await sdk.fetchJSON(api + '/task', { method: 'POST', body: JSON.stringify(body) });
        await refresh();
        if (result.status === 'queued') {
          setError('明确继续已排队，保留指令 ID：' + body.instruction_id);
        } else if (result.status === 'outcome_unknown' || (result.instruction && result.instruction.phase === 'rpc_intent')) {
          setError('控制结果待核对，保留指令 ID：' + body.instruction_id);
        } else {
          setControlIntents(Object.assign({}, controlIntents, { [key]: null }));
          if (action !== 'stop') setControlTexts(Object.assign({}, controlTexts, { [record.id]: '' }));
        }
      } catch (e) {
        const observed = await refresh();
        const task = observed && observed.requests.find(function (r) { return r.id === record.id; });
        const instruction = task && (task.controls || []).find(function (c) { return c.id === body.instruction_id; });
        if (task && !(task.queue && task.queue.pending_continuation) && (!instruction || instruction.phase === 'rejected')) {
          setControlIntents(Object.assign({}, controlIntents, { [key]: null }));
        }
        setError(String(e.message || e) + ' · 指令 ID：' + body.instruction_id);
      } finally { setSaving(false); }
    }
    function editProject(project) {
      const profile = snapshot.profiles.find(function (p) { return p.project_id === project.id; });
      setForm(Object.assign({}, blank, { projectId: project.id, name: project.name, repoPath: project.repo.worktree,
        artifacts: project.repo.test_artifact_paths.join('\n') }, profile ? {
        profileId: profile.id, nativeProfile: profile.native_profile, identityRef: profile.identity_ref,
        role: profile.role, capability: profile.capability, parent: profile.parent_profile_id || '',
        bot: profile.connection_refs.bot || '', credential: profile.connection_refs.credential || '',
        codex: profile.connection_refs.codex || '' } : {}));
      setReview(null);
    }
    const button = { padding: '7px 12px', border: '1px solid #8886', borderRadius: '6px',
      cursor: 'pointer', color: 'inherit', background: 'transparent' };
    return h('main', { style: { maxWidth: '1040px', margin: 'auto', padding: '24px', color: 'inherit' } },
      h('h1', null, '项目与 Profile'),
      h('p', null, '登记已有本地仓库和长期项目身份。开发执行与消息通道保持未启用，等待实际能力验证。'),
      h('button', { style: button, onClick: refresh }, '刷新目录'),
      error && h('p', { role: 'alert', style: { color: '#e66' } }, error),
      snapshot && h(React.Fragment, null,
        h('p', null, '运行：' + snapshot.runtime + ' · 配置版本：' + snapshot.version),
        h('p', null, '目录最后核实：' + (snapshot.last_verified_at || '尚未核实')),
        snapshot.intake_conditions && h('p', null, '消息入口：' + snapshot.intake_conditions.runtime_route +
          ' · 接缝兼容：' + snapshot.intake_conditions.compatibility + ' · 真实群验收：' + snapshot.intake_conditions.real_group_acceptance),
        snapshot.status === 'unverified' && h('p', { role: 'status' }, '管理实例离线，仅显示最后核实目录；修改未执行。'),
        h('h2', null, '组织目录'),
        h('ul', null, snapshot.projects.map(function (p) {
          return h('li', { key: p.id, style: { marginBottom: '10px' } }, h('strong', null, p.name + ' (' + p.id + ')'),
            h('div', null, p.repo.worktree), h('div', null, '逻辑 Git 公共目录：' + p.repo.common_dir),
            h('div', null, '嵌套只读仓库：' + p.repo.nested_repositories.length),
            h('button', { style: button, onClick: function () { editProject(p); } }, '修正登记'));
        })),
        h('ul', null, snapshot.profiles.map(function (p) {
          return h('li', { key: p.id }, p.id + ' · ' + p.role + ' · ' + p.capability + ' · 项目：' + (p.project_id || '独立'),
            h('div', null, '上级：' + (p.parent_profile_id || (p.role === 'project_lead' ? '项目根负责人（可合并两层）' : '项目树外')) + ' · 长期项目绑定：' + (p.project_id || '独立资料范围')),
            h('div', null, '原生 Profile 引用：' + p.native_profile + ' · 生命周期：配置中 · 执行：未启用'),
            h('div', null, '待验证：原生身份、新机器人、连接、凭据引用、执行接口和仓库权限。'));
        })),
        h('section', null, h('h2', null, '公开协作与任务关系'),
          h('p', null, '三层项目明确登记子负责人；合并两层由项目根负责人直接承接。发送、独立受理与执行分别核对。真实群验收：' + ((snapshot.collaboration || {}).real_group_acceptance || 'unverified')),
          h('ul', null, ((snapshot.collaboration || {}).handoffs || []).map(function (link) {
            const task = (snapshot.requests || []).find(function (r) { return r.id === link.task_request_id || r.id === link.result_task_id || r.id === link.direct_task_id; });
            return h('li', { key: link.id, style: { margin: '12px 0', overflowWrap: 'anywhere' } },
              h('strong', null, link.sender_profile_id + ' → ' + link.target_profile_id + ' · ' + link.kind),
              h('div', null, '关联：' + link.id + ' · 上级目标：' + (link.parent_handoff_id || link.original_handoff_id || '原本人目标')),
              h('div', null, '发送：' + link.delivery + ' · 独立受理：' + link.acceptance + ' · 执行：' + (task ? task.execution : '汇报不创建开发执行')),
              h('div', null, 'Issue：' + link.issue.url + ' · 原消息锚：' + link.source_anchor.chat_id + '/' + link.source_anchor.message_id),
              h('div', null, '接收锚：' + (link.received_anchor ? link.received_anchor.chat_id + '/' + link.received_anchor.message_id : '待核对') + ' · 任务确认锚：' + (task && task.task_start_anchor ? task.task_start_anchor.message_id : '待核对')),
              h('div', null, '集成：' + (link.integration_status || '待核对') + ' · 项目整体：待全局验证'),
              h('div', null, '原群身份绑定：' + (link.channel_binding || 'unverified') + (link.channel_reason ? ' · ' + link.channel_reason : '')),
              h('details', null, h('summary', null, '公开材料与逐段凭据'), h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(link.segments, null, 2))));
          })),
          h('details', null, h('summary', null, 'Owner 登记已核实路由与明确项目目标'),
            roleField('channels', '已有角色群路由数组 JSON（含原 app namespace、来源身份观察和 verification_ref）', true),
            h('button', { style: button, disabled: saving || snapshot.status === 'unverified', onClick: function () { previewRole('register_channels'); } }, '核对群路由'),
            roleField('sender', '已登记总管 Profile ID'), roleField('target', '明确目标总负责人 Profile ID'), roleField('issue', '明确项目 Issue URL'),
            roleField('anchor', '原本人消息锚 JSON', true),
            h('button', { style: button, disabled: saving || snapshot.status === 'unverified', onClick: function () { previewRole('project_goal'); } }, '核对公开项目交接'),
            roleReview && h('div', null, h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(roleReview, null, 2)),
              h('button', { style: button, disabled: saving || snapshot.status === 'unverified', onClick: applyRole }, '提交这条公开协作操作')))),
        h('section', null,
        h('h2', null, '手动 Codex 只观察'),
        h('p', null, '只读取已登记原执行器，保留手动会话；daemon、独立 CLI 与桌面分别核验，其他服务活动仍未知。'),
        h('button', { style: button, onClick: function () { observationAction(false); }, disabled: saving || snapshot.status !== 'completed' }, '核对手动会话'),
        h('ul', null, (snapshot.manual_capabilities || []).map(function (capability) { return h('li', { key: capability.kind }, capability.kind + '：' + capability.status); })),
        h('ul', null, (snapshot.manual_sources || []).map(function (source) { return h('li', { key: source.id },
          source.id + ' · ' + source.kind + ' · ' + source.status + ' · 权限 observe_only',
          source.reason && h('div', null, source.reason),
          h('details', null, h('summary', null, '原服务与可读取范围'), h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(source, null, 2)))); })),
        h('ul', null, (snapshot.manual_sessions || []).map(function (session) { return h('li', { key: session.id },
          session.source_kind + ' / ' + session.thread_id + '：' + session.state + ' · 只观察',
          h('div', null, '手动记录 ID：' + session.id + ' · 当前原 turn：' + (session.current_turn_id || '待本人核对')),
          h('div', null, '最后核实：' + session.last_verified_at + ' · 最后已知：' + (session.last_known_state || '未知')),
          h('div', null, '仓库：' + session.logical_repository + ' · 排队：' + (session.blocks_repository ? '等待手动执行或核实' : '本来源已核实无相关执行')),
          session.reason && h('div', null, session.reason)); })),
        h('details', null, h('summary', null, '登记已配置的原服务观察来源'),
          observerField('id', '来源稳定 ID'), observerField('kind', '来源类别', ['daemon', 'independent_cli', 'desktop']),
          observerField('projects', '已登记项目 ID（逗号分隔）'), observerField('adapter_ref', '已配置本地 adapter 引用'),
          h('button', { style: button, onClick: function () { observationAction(true); }, disabled: saving || snapshot.status !== 'completed' }, '登记来源'))),
        (snapshot.original_interface_requests || []).length > 0 && h('section', null, h('h2', null, '原服务需人工处理'),
          h('ul', null, snapshot.original_interface_requests.map(function (request) {
            return h('li', { key: request.generation + ':' + String(request.rpc_id) }, request.method + ' · 服务：' + request.service_id +
              ' · 请求：' + String(request.rpc_id) + ' · 状态：' + request.resolution,
              h('p', null, '该请求没有核实的任务对应。请在实际原客户端界面处理；安全链接尚不可用，勿在群中输入秘密。'));
          }))),
        h('h2', null, '已受理请求'),
        h('p', null, '任务与原群消息共用管理实例。执行能力按当前连接和权限证据核验；轮次结束、验收交付与 PR 状态分别显示。'),
        !(snapshot.requests || []).length && h('p', null, '尚无核实的受理记录。'),
        h('ul', null, (snapshot.requests || []).map(function (r) {
          const source = r.source_anchor;
          const anchor = r.task_start_anchor;
          const grant = (snapshot.control_grants || []).find(function (g) { return g.id === r.control_grant_id; });
          const controlled = r.session && r.session.control === 'assigned_task' && r.task_delivery !== 'delivered';
          const inputOpen = controlled && !r.repository_released && !['stopping', 'stopped'].includes(r.execution);
          const continueOpen = controlled && r.execution === 'stopped' && r.outer_task_status === 'stopped' &&
            r.stop && r.stop.status === 'confirmed' && r.repository_released && !(r.queue && r.queue.pending_continuation);
          return h('li', { key: r.id, style: { marginBottom: '18px' } },
            h('strong', null, r.project_id + ' · 负责人：' + r.profile_id),
            h('div', null, h('a', { href: r.accepted_scope.url, target: '_blank', rel: 'noreferrer' }, r.accepted_scope.title)),
            h('div', null, '受理：' + r.acceptance + ' · 消息送达：' + r.delivery + ' · 执行：' + r.execution),
            r.unexecuted_reason && h('div', null, '待核对原因：' + r.unexecuted_reason),
            r.queue && h('div', null, '仓库队列：' + r.queue.status + ' · 顺序：' + r.queue.sequence + ' · 安排时间：' + r.queue.arranged_at + (r.queue.reason ? ' · ' + r.queue.reason : '')),
            r.queue && r.queue.blocked_by.length > 0 && h('div', null, '等待前项：' + r.queue.blocked_by.join(', ')),
            (!r.session || r.repository_released) && h('details', null, h('summary', null, '确认本任务基线与工作区交接'),
              h('p', null, '由负责人按本 Issue、明确依赖及仓库约定填写。插件只核对现有工作区，遗留内容需按显示摘要确认保留。'),
              preparationField(r, 'branch', '已有本地分支'), preparationField(r, 'commit', '完整提交 SHA（空仓库填 unborn）'),
              preparationField(r, 'dependencies', '依赖请求 ID（逗号分隔）'), preparationField(r, 'workspace_digest', '遗留内容保留摘要（有未提交内容时填写）'),
              h('button', { style: button, onClick: function () { prepareAction(r); }, disabled: saving || snapshot.status !== 'completed' }, '确认基线'),
              r.preparation && h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(r.preparation, null, 2))),
            r.handoff_reason && h('div', null, '交付交接受阻：' + r.handoff_reason),
            h('div', null, '交付：' + (r.task_delivery || 'unmet') + ' · PR：' + (r.pr_status || 'none')),
            grant && h('div', null, '本次手动控制：' + grant.status + ' · 负责人：' + grant.controller_profile_id + ' · 原服务：' + grant.original_executor_id + ' · 授权：' + grant.id),
            grant && grant.reason && h('div', null, '原控制待核对：' + grant.reason),
            grant && h('button', { style: button, onClick: function () { manualGrantAction('return', r); }, disabled: saving || snapshot.status !== 'completed' || ['returned', 'completed'].includes(grant.status) }, '归还本次控制（不中断）'),
            (!r.session || grant && grant.status === 'returned') && h('details', null, h('summary', null, '本人接管此 Issue 的当前手动工作'),
              h('p', null, '指定本次请求的负责人控制已核实原会话。原服务或能力不支持时保持只观察，其他桌面操作识别仍未知。'),
              manualField(r, 'manual_session_id', '已观察手动记录 ID'), manualField(r, 'turn_id', '明确当前原 turn ID'),
              h('button', { style: button, onClick: function () { manualGrantAction('takeover', r); }, disabled: saving || snapshot.status !== 'completed' || !r.task_start_anchor }, '授权本次工作接管')),
            r.session && h('div', null, '原 Codex 会话：' + (r.session.thread_id || '创建待核对') + ' · 轮次：' + (r.session.turn_id || '启动待核对') + ' · 控制：' + r.session.control),
            r.last_execution_verified_at && h('div', null, '执行最后核实：' + r.last_execution_verified_at),
            r.recovery && h('section', null, h('div', null, '恢复对账：' + r.recovery.status + ' · 最后已确认执行：' + (r.recovery.last_confirmed_execution || '待核对')),
              h('div', null, '最后已确认时间：' + (r.recovery.last_confirmed_at || '待核对') + ' · 对账时间：' + r.recovery.checked_at),
              h('ul', null, (r.recovery.needs_human || []).map(function (reason) { return h('li', { key: reason }, reason); }))),
            r.session && h('button', { style: button, onClick: function () { taskAction('reconcile', r.id); }, disabled: saving || snapshot.status !== 'completed' }, '对账原任务与人工请求'),
            h('button', { style: button, onClick: function () { taskAction('verify', r.id); }, disabled: saving || snapshot.status !== 'completed' }, '核验执行能力'),
            h('button', { style: button, onClick: function () { taskAction(r.session ? 'refresh' : 'start', r.id); }, disabled: saving || snapshot.status !== 'completed' || !r.task_start_anchor }, r.session ? '核对原执行' : '启动已受理 Issue'),
            r.session && h('section', { style: { marginTop: '8px' } },
              h('div', null, '仓库占用：' + (r.repository_released ? '已释放' : '保留') + ' · 外层任务：' + (r.outer_task_status || '运行安排中')),
              h('label', null, '追加或明确继续的要求', h('input', { value: controlTexts[r.id] || '',
                onChange: function (e) { setControlTexts(Object.assign({}, controlTexts, { [r.id]: e.target.value })); },
                style: { margin: '6px', padding: '7px', color: 'inherit', background: 'transparent', border: '1px solid #8886' } })),
              h('button', { style: button, onClick: function () { controlAction('append', r); },
                disabled: saving || snapshot.status !== 'completed' || !r.session.turn_id || !String(controlTexts[r.id] || '').trim() || !inputOpen }, '追加到原会话'),
              h('button', { style: button, onClick: function () { controlAction('stop', r); },
                disabled: saving || snapshot.status !== 'completed' || !r.session.turn_id || !inputOpen }, '结束当前任务'),
              h('button', { style: button, onClick: function () { controlAction('continue', r); },
                disabled: saving || snapshot.status !== 'completed' || !continueOpen || !String(controlTexts[r.id] || '').trim() }, '明确继续原工作'),
              r.stop && h('div', null, '停止：' + r.stop.status + ' · 中断 RPC：' + r.stop.rpc_status +
                ' · 最后核实：' + (r.stop.last_verified_at || '待核对') + (r.stop.reason ? ' · ' + r.stop.reason : '')),
              (r.stop_records || []).length > 0 && h('details', null, h('summary', null, '停止记录、相关执行与继续安排'),
                h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify({ stops: r.stop_records,
                  arrangements: r.execution_arrangements || [], controls: r.controls || [] }, null, 2)))),
            (r.human_requests || []).map(function (q) {
              const reply = q.reply || {};
              const canAnswer = inputOpen && q.answerable && q.control_enabled && q.resolution === 'pending' &&
                !q.reply && !humanIntents[q.id] && snapshot.status === 'completed' && !saving;
              return h('section', { key: q.id, style: { margin: '12px 0', padding: '12px', border: '1px solid #8886', borderRadius: '6px' } },
                h('strong', null, '人工请求 · ' + q.category),
                h('div', null, '请求 ID：' + q.id),
                h('div', null, '收到：' + (reply.received ? '是' : '未答复') + ' · 送回：' + (reply.sent || '未送回') +
                  ' · 原请求已处理：' + q.resolution + ' · 执行结果：' + q.execution_result),
                h('div', null, '服务：' + q.service_id + ' · 会话：' + q.thread_id + ' · 轮次：' + (q.turn_id || '未提供') +
                  ' · 阻塞：' + (q.blocking === null ? '待核对' : q.blocking ? '是' : '否')),
                q.answerable && q.category !== 'approval' && (q.questions || []).map(function (item) {
                  const key = q.id + ':' + item.id;
                  return h('label', { key: item.id, style: { display: 'block', marginTop: '8px' } }, item.question,
                    h('input', { value: humanAnswers[key] || '', disabled: !canAnswer,
                      onChange: function (e) { setHumanAnswers(Object.assign({}, humanAnswers, { [key]: e.target.value })); },
                      list: item.options ? 'options-' + q.id + '-' + item.id : undefined,
                      style: { display: 'block', width: '100%', color: 'inherit', background: 'transparent', border: '1px solid #8886', padding: '7px' } }),
                    item.options && h('datalist', { id: 'options-' + q.id + '-' + item.id }, item.options.map(function (o) {
                      return h('option', { key: o.label, value: o.label }, o.description);
                    })));
                }),
                q.answerable && q.category === 'approval' && h(React.Fragment, null,
                  h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(q.operation, null, 2)),
                  h('div', null, '操作 ID：' + q.operation_id + ' · 批准范围：仅本回合（turn）'),
                  q.method === 'item/permissions/requestApproval' && h('label', null, '明确批准的权限 JSON 子集',
                    h('textarea', { value: humanAnswers[q.id + ':permissions'] || '', disabled: !canAnswer,
                      onChange: function (e) { setHumanAnswers(Object.assign({}, humanAnswers, { [q.id + ':permissions']: e.target.value })); },
                      style: { display: 'block', width: '100%', color: 'inherit', background: 'transparent' } })),
                  h('label', null, h('input', { type: 'checkbox', checked: !!humanReviewed[q.id], disabled: !canAnswer,
                    onChange: function (e) { setHumanReviewed(Object.assign({}, humanReviewed, { [q.id]: e.target.checked })); } }),
                    '本人已核对上述具体操作与仅本回合的授权范围'),
                  h('div', null, h('button', { style: button, disabled: !canAnswer || !humanReviewed[q.id],
                    onClick: function () { answerHuman(r, q, 'accept'); } }, '批准此操作（仅本回合）'),
                    h('button', { style: button, disabled: !canAnswer || !humanReviewed[q.id],
                    onClick: function () { answerHuman(r, q, 'decline'); } }, '拒绝此操作'))),
                q.answerable && q.category !== 'approval' && h('button', { style: button,
                  disabled: !canAnswer || !(q.questions || []).every(function (item) { return String(humanAnswers[q.id + ':' + item.id] || '').trim(); }),
                  onClick: function () { answerHuman(r, q); } }, '将本人答案送回原请求'),
                !q.answerable && h('p', null, '请在原服务的安全原界面处理。原会话：' + q.thread_id +
                  '；安全链接尚不可用。不要在群或此表单输入秘密答案。'),
                q.answerable && !q.control_enabled && h('p', null, '原连接人工答复能力尚未核验；请定位原界面。'),
                (q.reply || humanIntents[q.id]) && h('button', { style: button, onClick: function () { taskAction('refresh', r.id); }, disabled: saving }, '核对原请求与执行结果'));
            }),
            r.execution_capability && h('div', null, '启动能力：' + r.execution_capability.status + (r.execution_capability.reason ? ' · ' + r.execution_capability.reason : '')),
            h('button', { style: button, onClick: function () { taskAction('source', r.id); }, disabled: saving || snapshot.status !== 'completed' }, '核对 Issue 来源'),
            r.issue_source && h('details', null, h('summary', null, 'Issue 来源：' + r.issue_source.status + ' · 已受理版本保留'),
              h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(r.issue_source, null, 2))),
            r.delivery_evidence && h('details', null, h('summary', null, '验收与测试／Git／PR 证据'), h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(r.delivery_evidence, null, 2))),
            h('div', null, '请求 ID：' + r.id),
            h('div', null, '来源群 / 消息：' + source.chat_id + ' / ' + source.message_id),
            h('div', null, '任务起始锚：' + (anchor ? anchor.chat_id + ' / ' + anchor.message_id : '待核对')),
            h('div', null, 'Issue 受理版本：' + r.accepted_scope.updated_at + ' · 受理时间：' + r.accepted_at),
            h('details', null, h('summary', null, '已受理范围与逐段投递凭据'),
              h('pre', { style: { whiteSpace: 'pre-wrap' } }, r.accepted_scope.body),
              h('ul', null, r.outbox.map(function (p) {
                return h('li', { key: p.id }, p.kind, h('ul', null, p.segments.map(function (s) {
                  const receipt = s.attempts[s.attempts.length - 1] || {};
                  return h('li', { key: s.uuid }, '第 ' + s.number + ' 段 · ' + s.status +
                    ' · 尝试：' + s.attempts.length + ' · 消息：' + (receipt.message_id || '待核对'));
                })));
              }))));
        })),
        (snapshot.clarifications || []).length > 0 && h('section', null, h('h2', null, '待澄清输入'),
          h('ul', null, snapshot.clarifications.map(function (c) {
            return h('li', { key: c.id }, c.source_anchor.chat_id + ' / ' + c.source_anchor.message_id +
              ' · 候选请求：' + c.candidate_ids.length + ' · 澄清回复送达：' + c.delivery);
          }))),
        (snapshot.intake_failures || []).length > 0 && h('section', null, h('h2', null, '受理需核对'),
          h('ul', null, snapshot.intake_failures.map(function (f) {
            return h('li', { key: f.id }, f.source_anchor.chat_id + ' / ' + f.source_anchor.message_id +
              ' · 受理：' + f.acceptance + ' · 通知：' + f.notification.status,
              h('div', null, f.reason));
          }))),
        h('section', null, h('h2', null, '资料来源与查询'),
          h('p', null, '按实际提问者和明确分享范围查询，原资料库只读。资料不变成新授权；迟到结果只展示材料。'),
          h('ul', null, (snapshot.knowledge_sources || []).map(function (source) {
            return h('li', { key: source.id }, source.name + ' · ' + source.id + ' · Wiki：' + source.wiki_profile_id,
              h('details', null, h('summary', null, '查询主体与公开范围'), h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(source, null, 2))));
          })),
          h('ul', null, (snapshot.knowledge_queries || []).map(function (query) {
            return h('li', { key: query.id, style: { marginBottom: '14px' } }, query.id + ' · ' + query.source_id + ' · ' + query.status,
              h('div', null, '实际提问者：' + query.requester + ' · 范围：' + query.scope_ids.join(', ') + ' · 原任务：' + (query.request_id || '独立查询')),
              h('div', null, '结果关联：' + (query.result_anchor ? query.result_anchor.chat_id + ' / ' + query.result_anchor.message_id : '待核对')),
              h('ul', null, (query.materials || []).map(function (material) { return h('li', { key: material.id }, '[' + material.kind + '] ' + material.text,
                h('div', null, material.locator + ' · ' + material.version + ' · ' + material.updated_at)); })),
              query.supplement && h('div', null, '原会话事实补充：' + query.supplement.status + ' · ' + (query.supplement.reason || '执行结果仍需核对')),
              h('details', null, h('summary', null, '公开查询与逐段凭据'), h('pre', { style: { whiteSpace: 'pre-wrap' } }, JSON.stringify(query.outbox || [], null, 2))));
          }))),
        h('h2', null, '旧档案与数据检查点'),
        h('p', null, '查询仅按登记迁移和共享范围读取。原入口保持停用；外部服务恢复能力须单独核验。'),
        h('ul', null, (snapshot.archive_sources || []).map(function (source) {
          return h('li', { key: source.id }, source.id + ' · ' + source.kind + ' · 迁移 Profile：' + source.new_profile_id,
            h('details', null, h('summary', null, '授权与原生清理保护'), h('pre', null, JSON.stringify(source, null, 2))));
        })),
        h('ul', null, (snapshot.archive_queries || []).map(function (query) {
          return h('li', { key: query.id }, query.id + ' · ' + query.source_id + ' · ' + query.status,
            h('div', null, query.reason || ''),
            h('pre', null, JSON.stringify(query.coverage || {}, null, 2)),
            h('ul', null, (query.records || []).map(function (row) {
              return h('li', { key: row.locator }, row.locator + ' · ' + row.timestamp + ' · ' + query.source_version, h('p', null, row.text));
            })), h('details', null, h('summary', null, '原提问与逐段反馈凭据'), h('pre', null, JSON.stringify(query.outbox || [], null, 2))));
        })),
        h('details', null, h('summary', null, '每日副本、长期基线与恢复后查询'),
          h('pre', null, JSON.stringify({ backups: snapshot.archive_backups || [], restores: snapshot.archive_restores || [] }, null, 2))),
        h('h2', null, '登记或修正'),
        h('p', null, '这里只保存非敏感引用；不创建原生 Profile、机器人、仓库或 worktree。项目身份不能改绑到新项目。'),
        h('form', { onSubmit: preview },
          h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: '12px' } },
            field('projectId', '项目稳定 ID'), field('name', '项目名称'), field('repoPath', '已有仓库绝对路径'),
            field('profileId', 'Profile 稳定 ID（可选）'), field('nativeProfile', '原生 Profile 引用'),
            field('identityRef', '已核验身份引用'),
            field('role', '责任角色', [['steward', '总管'], ['project_lead', '项目总负责人'], ['subproject_lead', '子项目负责人'], ['independent', '独立助手']]),
            field('capability', '能力分类', [['development', '开发型'], ['non_development', '非开发型']]),
            field('parent', '上级 Profile ID（子负责人必填）'), field('bot', '机器人引用（identity:...）'),
            field('credential', '原生凭据引用（native:...）'), field('codex', '本机服务引用（local:...）')),
          h('label', { style: { display: 'block', margin: '12px 0' } }, '允许测试产物绝对路径（每行一个）',
            h('textarea', { value: form.artifacts, onChange: function (e) { setForm(Object.assign({}, form, { artifacts: e.target.value })); setReview(null); },
              style: { width: '100%', color: 'inherit', background: 'transparent', border: '1px solid #8886' } })),
          h('button', { type: 'submit', style: button, disabled: snapshot.status !== 'completed' || saving }, '预览变更')),
        review && h('section', null, h('h3', null, '待提交变更'),
          h('p', null, '提交仅更新目录；身份和实际能力仍需验证，不会开始执行。版本冲突时请刷新并重新确认。'),
          h('pre', { style: { overflowX: 'auto', padding: '12px', background: '#8881' } }, JSON.stringify(review, null, 2)),
          h('button', { style: button, onClick: apply, disabled: saving }, saving ? '提交中…' : '确认目录变更'))));
  }
  window.__HERMES_PLUGINS__.register('ghost-hermes-pm', Projects);
}());
