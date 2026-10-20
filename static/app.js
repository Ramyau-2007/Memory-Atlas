'use strict';

let token = localStorage.getItem('sessionToken');
let conversationId = null;
let conversations = [];
let pendingRenameId = null;
let conversationLoadRequest = 0;
let allMemories = [];
let universeMemories = [];
let activeView = 'chat';
let actionHandler = null;
let toastTimer;
let messageEditPending = false;
let invitationRequired = false;
let authMode = null;

const byId = (id) => document.getElementById(id);
// This element is moved in and out of #messages as conversations are switched.
// Keep its reference because getElementById cannot find a detached DOM node.
const welcomeState = byId('welcomeState');
if (!token) { byId('sidebar').hidden = true; document.querySelector('.main-shell').style.marginLeft = '0'; }

async function api(path, options = {}) {
  const response = await fetch('/api' + path, {
    ...options,
    credentials: 'same-origin',
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {})
    }
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function toast(message, kind = 'success') {
  const region = byId('toastRegion');
  region.replaceChildren();
  const element = document.createElement('div');
  element.className = `toast${kind === 'error' ? ' error' : ''}`;
  element.textContent = message;
  region.append(element);
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => element.remove(), 4200);
}

function showStatus(message, kind = 'success') {
  const target = byId('statusMessage');
  target.textContent = message;
  target.className = `status-message ${kind}`;
  target.hidden = false;
}

function clearStatus() { byId('statusMessage').hidden = true; }

function handleAuthError(error) {
  if (/sign in|session|unauthori[sz]ed/i.test(error.message)) {
    localStorage.removeItem('sessionToken');
    token = null;
    byId('workspace').hidden = true;
    byId('authPanel').hidden = false;
    byId('sidebar').hidden = true;
    document.querySelector('.main-shell').style.marginLeft = '0';
    byId('authMessage').textContent = 'Your session has ended. Please sign in again.';
  } else {
    toast(error.message, 'error');
  }
}

async function auth(kind) {
  const message = byId('authMessage');
  message.textContent = '';
  const username = byId('username').value.trim();
  const password = byId('password').value;
  try {
    if (kind === 'register') {
      const invitationCode = byId('invitationCode').value.trim();
      if (invitationRequired && !invitationCode) {
        message.textContent = 'Enter the demo invitation code provided by the organizer.';
        byId('invitationCode').focus();
        return;
      }
      await api('/register', { method: 'POST', body: JSON.stringify({ username, password, invitation_code: invitationCode || null }) });
      byId('password').value = '';
      byId('invitationCode').value = '';
      setAuthMode('login');
      message.textContent = 'Account created. Sign in with your new credentials.';
      byId('username').focus();
      return;
    }
    const result = await api('/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    token = result.token || null;
    if (token) localStorage.setItem('sessionToken', token);
    else localStorage.removeItem('sessionToken');
    await showWorkspace();
  } catch (error) { message.textContent = error.message; }
}

function setAuthMode(mode) {
  authMode = mode;
  byId('authChoice').hidden = true;
  byId('authForm').hidden = false;
  byId('authSubmit').textContent = mode === 'register' ? 'Create New Account' : 'Sign In';
  byId('authSwitch').textContent = mode === 'register' ? 'Already have an account? Sign In' : 'Create New Account';
  byId('password').autocomplete = mode === 'register' ? 'new-password' : 'current-password';
  byId('authMessage').textContent = '';
  byId('username').focus();
}

byId('chooseRegister').addEventListener('click', () => setAuthMode('register'));
byId('chooseLogin').addEventListener('click', () => setAuthMode('login'));
byId('authSwitch').addEventListener('click', () => setAuthMode(authMode === 'register' ? 'login' : 'register'));
byId('authForm').addEventListener('submit', (event) => {
  event.preventDefault();
  auth(authMode || 'login');
});

async function showWorkspace() {
  byId('authPanel').hidden = true;
  byId('workspace').hidden = false;
  byId('sidebar').hidden = false;
  document.querySelector('.main-shell').style.marginLeft = '';
  clearStatus();
  try { await loadProvider(); } catch (error) { toast(error.message, 'error'); }
  await Promise.all([loadMemories(), loadUniverse(), loadTimeline(), loadAnalytics(), loadConversations()]);
}

async function logout() {
  try { await api('/logout', { method: 'POST' }); }
  catch (error) { toast(`Sign-out could not be confirmed: ${error.message}`, 'error'); return; }
  localStorage.removeItem('sessionToken');
  localStorage.removeItem('activeConversationId');
  token = null;
  conversationId = null;
  location.reload();
}
byId('signOut').addEventListener('click', logout);

function renderConversationList() {
  const list = byId('conversationList'); list.replaceChildren();
  if (!conversations.length) {
    const empty = document.createElement('p'); empty.className = 'conversation-list-empty'; empty.textContent = 'Your chats will appear here.'; list.append(empty); return;
  }
  for (const conversation of conversations) {
    const row = document.createElement('div'); row.className = `conversation-row${conversation.id === conversationId ? ' active' : ''}`;
    const select = document.createElement('button'); select.type = 'button'; select.className = 'conversation-select';
    select.textContent = conversation.title; select.title = conversation.title;
    select.setAttribute('aria-label', `Open conversation: ${conversation.title}`);
    if (conversation.id === conversationId) select.setAttribute('aria-current', 'true');
    select.addEventListener('click', () => selectConversation(conversation.id));
    const menu = document.createElement('details'); menu.className = 'conversation-menu';
    const summary = document.createElement('summary'); summary.textContent = '⋯'; summary.setAttribute('aria-label', `Options for ${conversation.title}`); summary.title = 'Conversation options';
    const actions = document.createElement('div'); actions.className = 'conversation-menu-actions';
    const rename = document.createElement('button'); rename.type = 'button'; rename.textContent = 'Rename';
    rename.addEventListener('click', () => { menu.open = false; openRenameConversation(conversation); });
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Delete'; remove.className = 'conversation-delete';
    remove.addEventListener('click', () => { menu.open = false; confirmDeleteConversation(conversation); });
    actions.append(rename, remove); menu.append(summary, actions); row.append(select, menu); list.append(row);
  }
}

async function loadConversations(preferredId = null, reloadSelected = true) {
  try {
    conversations = await api('/conversations');
    const requested = preferredId ?? Number(localStorage.getItem('activeConversationId'));
    const selected = conversations.find((item) => item.id === requested) || conversations[0];
    renderConversationList();
    if (selected && !reloadSelected && selected.id === conversationId) renderConversationList();
    else if (selected) await selectConversation(selected.id, false);
    else { conversationId = null; localStorage.removeItem('activeConversationId'); byId('messages').replaceChildren(); byId('messages').append(welcomeState); welcomeState.hidden = false; renderConversationList(); }
  } catch (error) {
    byId('conversationList').textContent = `Could not load chat history: ${error.message}`;
  }
}

async function refreshConversationList() {
  try { conversations = await api('/conversations'); renderConversationList(); }
  catch (error) { toast(`Could not refresh chat history: ${error.message}`, 'error'); }
}

async function selectConversation(id, closeSidebar = true) {
  const conversation = conversations.find((item) => item.id === id);
  if (!conversation) return;
  const loadRequest = ++conversationLoadRequest;
  const target = byId('messages');
  target.replaceChildren();
  const loading = document.createElement('p'); loading.className = 'conversation-list-empty'; loading.textContent = 'Loading conversation…'; target.append(loading);
  try {
    const messages = await api(`/conversations/${id}/messages`);
    if (loadRequest !== conversationLoadRequest) return;
    conversationId = id; localStorage.setItem('activeConversationId', String(id));
    byId('inspectorContent').replaceChildren(addInspectorSection('CONTEXT INSPECTOR', 'No saved context trace is available for this earlier response. Details will appear after your next message.'));
    target.replaceChildren();
    if (!messages.length) { target.append(welcomeState); welcomeState.hidden = false; }
    else for (const message of messages) if (message.role === 'user' || message.role === 'assistant') appendMessage(message.role, message.content, '', message.id);
    renderConversationList();
    if (closeSidebar) { navigate('chat'); byId('query').focus(); }
  } catch (error) {
    if (loadRequest !== conversationLoadRequest) return;
    target.replaceChildren(); const message = document.createElement('p'); message.className = 'conversation-list-empty'; message.textContent = `Could not load this conversation: ${error.message}`; target.append(message); toast(error.message, 'error');
  }
}

async function createConversation() {
  try {
    const created = await api('/conversations', { method: 'POST', body: JSON.stringify({ title: 'New chat' }) });
    await loadConversations(created.id); navigate('chat'); byId('query').focus();
    byId('sidebar').classList.remove('open'); byId('menuToggle').setAttribute('aria-expanded', 'false');
  } catch (error) { toast(`Could not create a chat: ${error.message}`, 'error'); }
}
byId('newChat').addEventListener('click', createConversation);

function openRenameConversation(conversation) {
  pendingRenameId = conversation.id; byId('conversationTitleInput').value = conversation.title;
  byId('conversationRenameMessage').textContent = ''; byId('conversationRenameDialog').showModal(); byId('conversationTitleInput').focus();
}
byId('conversationRenameForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (event.submitter?.value === 'cancel') { byId('conversationRenameDialog').close(); return; }
  const title = byId('conversationTitleInput').value.trim();
  if (!title) { byId('conversationRenameMessage').textContent = 'Enter a title before saving.'; return; }
  const save = byId('saveConversationTitle'); save.disabled = true;
  try {
    await api(`/conversations/${pendingRenameId}`, { method: 'PATCH', body: JSON.stringify({ title }) });
    byId('conversationRenameDialog').close(); toast('Conversation renamed.'); await loadConversations(pendingRenameId);
  } catch (error) { byId('conversationRenameMessage').textContent = error.message; }
  finally { save.disabled = false; }
});

function confirmDeleteConversation(conversation) {
  openAction({ title: 'Delete this conversation?', description: `“${conversation.title}” and its messages will be permanently deleted from this database. Saved long-term memories are not affected. This cannot be undone.`, destructive: true, confirmLabel: 'Delete conversation', run: async () => {
    try {
      await api(`/conversations/${conversation.id}`, { method: 'DELETE' });
      if (conversation.id === conversationId) { conversationId = null; localStorage.removeItem('activeConversationId'); }
      toast('Conversation and its messages deleted. Saved memories were kept.'); await loadConversations();
    } catch (error) { toast(error.message, 'error'); }
  }});
}

const viewInfo = {
  chat: ['AI Assistant', 'YOUR PRIVATE WORKSPACE', 'A conversation shaped by the context you choose to keep.'],
  universe: ['Memory Universe', 'THE SHAPE OF YOUR CONTEXT', 'Explore stored memories and the words that connect them.'],
  memories: ['All Memories', 'YOUR PERSONAL ARCHIVE', 'Review, refine, and manage what stays in your context.'],
  timeline: ['Memory Timeline', 'A RECORD OF CHANGE', 'See memory versions and lifecycle events that were actually recorded.'],
  analytics: ['Analytics', 'YOUR MEMORY AT A GLANCE', 'A clear view of the records in your account.'],
  settings: ['Settings & About', 'CONFIGURATION & CONTROL', 'Understand the response provider and the boundaries of this local POC.']
};

function navigate(view) {
  if (!viewInfo[view]) return;
  activeView = view;
  document.querySelectorAll('.nav-item[data-view]').forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle('active', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.querySelectorAll('.view').forEach((section) => {
    const selected = section.id === `view-${view}`;
    section.classList.toggle('active', selected);
    section.hidden = !selected;
  });
  const [title, eyebrow, description] = viewInfo[view];
  byId('pageTitle').textContent = title;
  byId('pageCrumb').textContent = title;
  byId('pageEyebrow').textContent = eyebrow;
  byId('pageDescription').textContent = description;
  byId('sidebar').classList.remove('open');
  byId('menuToggle').setAttribute('aria-expanded', 'false');
  if (view === 'universe') loadUniverse();
  if (view === 'memories') loadMemories();
  if (view === 'timeline') loadTimeline();
  if (view === 'analytics') loadAnalytics();
}
document.querySelectorAll('.nav-item[data-view]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.view)));
byId('menuToggle').addEventListener('click', () => {
  const open = byId('sidebar').classList.toggle('open');
  byId('menuToggle').setAttribute('aria-expanded', String(open));
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') byId('sidebar').classList.remove('open');
  if (/^[1-5]$/.test(event.key) && !event.ctrlKey && !event.altKey && !event.metaKey && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) && !byId('workspace').hidden) {
    const view = ['chat', 'universe', 'memories', 'timeline', 'analytics'][Number(event.key) - 1];
    navigate(view);
  }
});

async function loadProvider() {
  const data = await api('/health');
  const deterministic = data.provider === 'deterministic';
  const label = deterministic ? 'Deterministic development mode' : `${data.provider} · ${data.model}`;
  byId('providerLabel').textContent = deterministic ? 'Development mode' : `${data.provider} · ${data.model}`;
  byId('providerDot').parentElement.classList.remove('ready');
  byId('settingsProvider').textContent = deterministic ? 'Deterministic development mode' : data.provider;
  byId('settingsModel').textContent = data.model;
  byId('settingsProviderBadge').textContent = label;
  byId('providerNotice').className = `notice${deterministic ? ' warm-notice' : ''}`;
  byId('providerNotice').replaceChildren();
  const icon = document.createElement('span'); icon.textContent = 'i';
  const note = document.createElement('p');
  note.textContent = deterministic
    ? 'This provider returns clearly labeled development responses; it does not generate answers with an LLM.'
    : 'This is the configured provider and model. Service availability is only known after a chat request; no connection check is implied here.';
  byId('providerNotice').append(icon, note);
}

function addMessageEditControl(item, messageId) {
  item.dataset.messageId = String(messageId);
  if (item.querySelector('.message-edit-button')) return;
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'message-edit-button';
  button.textContent = 'Edit'; button.setAttribute('aria-label', 'Edit this message');
  button.addEventListener('click', () => beginMessageEdit(item));
  item.querySelector('.message-body').append(button);
}

function beginMessageEdit(item) {
  if (messageEditPending || item.querySelector('.message-edit-form')) return;
  const text = item.querySelector('.message-text').textContent;
  const editor = document.createElement('form'); editor.className = 'message-edit-form';
  const textarea = document.createElement('textarea'); textarea.required = true;
  textarea.maxLength = 4000; textarea.value = text; textarea.setAttribute('aria-label', 'Edit your message');
  const feedback = document.createElement('p'); feedback.className = 'message-edit-error'; feedback.hidden = true;
  const actions = document.createElement('div'); actions.className = 'message-edit-actions';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'mini-button'; cancel.textContent = 'Cancel';
  const save = document.createElement('button'); save.type = 'submit'; save.className = 'mini-button message-edit-save'; save.textContent = 'Save and regenerate';
  cancel.addEventListener('click', () => editor.remove());
  actions.append(cancel, save); editor.append(textarea, feedback, actions);
  item.querySelector('.message-text').hidden = true;
  item.querySelector('.message-edit-button').hidden = true;
  item.querySelector('.message-body').append(editor); textarea.focus(); textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  editor.addEventListener('submit', async (event) => {
    event.preventDefault();
    const revised = textarea.value.trim();
    if (!revised) { feedback.textContent = 'Message cannot be empty.'; feedback.hidden = false; return; }
    const id = item.dataset.messageId;
    if (!conversationId || !id || messageEditPending) return;
    const editedConversationId = conversationId;
    messageEditPending = true; save.disabled = true; cancel.disabled = true; textarea.disabled = true;
    save.textContent = 'Regenerating…';
    try {
      const result = await api(`/conversations/${editedConversationId}/messages/${id}`, {
        method: 'PATCH', body: JSON.stringify({ message: revised })
      });
      if (result.edited) {
        if (conversationId === editedConversationId) {
          await loadConversations(editedConversationId);
          renderInspector(result, revised);
        } else await refreshConversationList();
        toast(result.preserved_user_edited_memories?.length
          ? 'Message regenerated. A manually revised linked memory was preserved for your review.'
          : 'Message edited and response regenerated.');
        await Promise.all([loadMemories(), loadUniverse(), loadTimeline(), loadAnalytics()]);
      }
    } catch (error) {
      feedback.textContent = `Could not regenerate this response: ${error.message}`;
      save.textContent = 'Save and regenerate';
      feedback.hidden = false; save.disabled = false; cancel.disabled = false; textarea.disabled = false; textarea.focus();
    } finally { messageEditPending = false; }
  });
}

function appendMessage(role, text, extraClass = '', messageId = null) {
  const item = document.createElement('article');
  item.className = `chat-message ${role}${extraClass ? ` ${extraClass}` : ''}`;
  const avatar = document.createElement('span'); avatar.className = 'message-avatar'; avatar.textContent = role === 'user' ? 'Y' : 'M';
  avatar.setAttribute('aria-hidden', 'true');
  const body = document.createElement('div'); body.className = 'message-body';
  const label = document.createElement('span'); label.className = 'message-label'; label.textContent = role === 'user' ? 'YOU' : 'MEMORY ATLAS';
  const content = document.createElement('div'); content.className = 'message-text';
  if (role === 'assistant' && !extraClass.includes('thinking') && !extraClass.includes('message-error')) {
    renderAssistantMarkdown(content, text);
  } else content.textContent = text;
  body.append(label, content);
  item.append(avatar, body);
  if (role === 'user' && messageId !== null) addMessageEditControl(item, messageId);
  welcomeState.hidden = true;
  byId('messages').append(item);
  byId('messages').scrollTop = byId('messages').scrollHeight;
  return item;
}

function renderAssistantMarkdown(element, value) {
  const source = String(value ?? '').replace(
    /&lt;(\/?(?:br|p|ul|ol|li|h[1-6]|strong|em|pre|table|thead|tbody|tr|th|td|blockquote)\s*\/?)&gt;/gi,
    '<$1>'
  );
  if (!window.marked || !window.DOMPurify) { element.textContent = String(value ?? ''); return; }
  const parsed = window.marked.parse(source, { gfm: true, breaks: false });
  const safe = window.DOMPurify.sanitize(parsed, {
    ALLOWED_TAGS: ['p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'strong', 'em', 'del', 'hr', 'a'],
    ALLOWED_ATTR: ['href', 'title', 'class', 'align'],
    ALLOWED_URI_REGEXP: /^(?:(?:https?:\/\/|mailto:)|(?:\/(?!\/)|\.{1,2}\/|#))/i,
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'img', 'form', 'input', 'video', 'audio'],
    FORBID_ATTR: ['style', 'target']
  });
  // This is the only HTML insertion in message rendering. `safe` is DOMPurify
  // output with an explicit Markdown-only tag and attribute allowlist.
  element.innerHTML = safe;
}

function addInspectorSection(title, content) {
  const section = document.createElement('section'); section.className = 'inspector-section';
  const heading = document.createElement('h3'); heading.textContent = title;
  section.append(heading);
  if (typeof content === 'string') {
    const paragraph = document.createElement('p'); paragraph.textContent = content; section.append(paragraph);
  } else section.append(content);
  return section;
}

function renderInspector(result, submitted) {
  const target = byId('inspectorContent'); target.replaceChildren();
  const providerText = result.provider === 'deterministic'
    ? 'Deterministic development response (not LLM output)'
    : `${result.provider} · ${result.model}`;
  target.append(addInspectorSection('RESPONSE SOURCE', providerText));
  target.append(addInspectorSection('YOUR MESSAGE', submitted));
  const retrieved = document.createElement('ul');
  if (result.retrieved.length) {
    for (const memory of result.retrieved) {
      const li = document.createElement('li'); li.textContent = memory.content;
      const why = document.createElement('span'); why.textContent = memory.retrieval_reason || 'Retrieved for this request.';
      li.append(why); retrieved.append(li);
    }
  } else {
    const li = document.createElement('li'); li.textContent = 'No active saved memories matched this request.'; retrieved.append(li);
  }
  target.append(addInspectorSection(`RETRIEVED MEMORIES · ${result.retrieved.length}`, retrieved));
  const source = result.context;
  const estimate = document.createElement('div'); estimate.className = 'metric-pills';
  for (const label of [`Recent-context baseline estimate: ${source.baseline_tokens_est} tokens`, `Selected context estimate: ${source.proposed_tokens_est} tokens`, `Retrieval: ${result.timings.retrieval_ms} ms`, `Generation: ${result.timings.generation_ms} ms`]) {
    const pill = document.createElement('span'); pill.className = 'metric-pill'; pill.textContent = label; estimate.append(pill);
  }
  target.append(addInspectorSection('MEASUREMENTS · ESTIMATES LABELED', estimate));
  target.append(addInspectorSection('MEMORY DECISION', result.memory_decision.reason));
  const detail = document.createElement('details');
  const summary = document.createElement('summary'); summary.textContent = 'Inspect context sent to the adapter';
  detail.append(summary);
  if (source.adapter_input) {
    const pre = document.createElement('pre');
    pre.textContent = typeof source.adapter_input === 'string' ? source.adapter_input : JSON.stringify(source.adapter_input, null, 2);
    detail.append(pre);
  } else {
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify({ query: source.query, retrieved_memories: source.memories_supplied, recent_context: source.recent_context_supplied, note: source.deterministic_note }, null, 2);
    detail.append(pre);
  }
  target.append(addInspectorSection('ACTUAL ADAPTER INPUT', detail));
  const note = document.createElement('p'); note.textContent = source.note; note.className = 'muted'; note.style.fontSize = '8px';
  target.append(note);
}

byId('chatForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = byId('query'); const message = input.value.trim();
  if (!message) return;
  const submittedConversationId = conversationId;
  input.value = ''; input.disabled = true; byId('send').disabled = true;
  const userMessage = appendMessage('user', message);
  const pending = appendMessage('assistant', 'Gathering the context for this request…', 'thinking');
  try {
    const result = await api('/chat', { method: 'POST', body: JSON.stringify({ message, ...(submittedConversationId ? { conversation_id: submittedConversationId } : {}) }) });
    pending.remove();
    const stillSelected = conversationId === submittedConversationId;
    if (stillSelected) {
      conversationId = result.conversation_id;
      localStorage.setItem('activeConversationId', String(conversationId));
      if (result.message_id) addMessageEditControl(userMessage, result.message_id);
      appendMessage('assistant', result.answer);
      renderInspector(result, message);
    }
    showStatus(`Memory decision: ${result.memory_decision.reason}`, 'success');
    await Promise.all([loadMemories(), loadUniverse(), loadTimeline(), loadAnalytics(), refreshConversationList()]);
  } catch (error) {
    pending.classList.add('message-error');
    pending.querySelector('.message-label').textContent = 'REQUEST FAILED';
    pending.querySelector('.message-text').textContent = error.message;
    if (error.message.toLowerCase().includes('ollama')) {
      byId('inspectorContent').replaceChildren(addInspectorSection('MODEL REQUEST FAILED', error.message));
    }
    await refreshConversationList();
  } finally {
    input.disabled = false; byId('send').disabled = false; input.focus();
  }
});
byId('query').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); byId('chatForm').requestSubmit(); }
});
document.querySelectorAll('.suggestion').forEach((button) => button.addEventListener('click', () => {
  byId('query').value = button.dataset.prompt;
  byId('query').focus();
}));

function dateLabel(value) {
  if (!value) return 'Timestamp unavailable';
  const parsed = new Date(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`);
  return Number.isNaN(parsed.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(parsed);
}
function timestampValue(value) {
  if (!value) return 0;
  const parsed = new Date(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`);
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
}
function categoryClass(category) {
  const value = String(category || 'fact').toLowerCase();
  return ['preference', 'goal', 'project'].includes(value) ? value : 'fact';
}
function categoryTag(category) {
  const tag = document.createElement('span'); tag.className = `category-tag ${categoryClass(category)}`; tag.textContent = category || 'fact'; return tag;
}

async function loadMemories() {
  const deleted = byId('memoryStatus').value === 'deleted';
  const query = byId('memorySearch').value.trim();
  byId('memoryResultCount').textContent = 'Loading…';
  try {
    allMemories = await api(`${deleted ? '/memories/deleted' : '/memories'}?q=${encodeURIComponent(query)}`);
    renderMemoryList();
  } catch (error) { byId('memoryResultCount').textContent = 'Could not load memories'; handleAuthError(error); }
}

function updateCategorySelect(select, records, selectedValue = select.value) {
  const values = [...new Set(records.map((item) => item.category).filter(Boolean))].sort();
  const first = select.firstElementChild;
  select.replaceChildren(first);
  for (const value of values) {
    const option = document.createElement('option'); option.value = value; option.textContent = value; select.append(option);
  }
  select.value = values.includes(selectedValue) ? selectedValue : '';
}

function renderMemoryList() {
  const target = byId('memoryList'); target.replaceChildren();
  updateCategorySelect(byId('memoryCategory'), allMemories);
  const category = byId('memoryCategory').value;
  const visible = (category ? allMemories.filter((item) => item.category === category) : [...allMemories]);
  const sort = byId('memorySort').value;
  visible.sort((left, right) => {
    if (sort === 'oldest') return timestampValue(left.created_at) - timestampValue(right.created_at);
    if (sort === 'priority') return Number(right.priority || 0) - Number(left.priority || 0) || timestampValue(right.updated_at) - timestampValue(left.updated_at);
    if (sort === 'alphabetical') return String(left.content).localeCompare(String(right.content), undefined, { sensitivity: 'base' });
    return timestampValue(right.updated_at || right.created_at) - timestampValue(left.updated_at || left.created_at);
  });
  byId('memoryResultCount').textContent = `${visible.length} ${visible.length === 1 ? 'memory' : 'memories'}${byId('memoryStatus').value === 'deleted' ? ' in recovery' : ''}`;
  if (!visible.length) {
    const empty = document.createElement('div'); empty.className = 'empty-state';
    empty.textContent = byId('memoryStatus').value === 'deleted' ? 'The recovery area is empty. Soft-deleted memories will appear here.' : 'Nothing saved here yet. Add a useful preference or let the assistant identify one from a conversation.';
    target.append(empty); return;
  }
  for (const item of visible) target.append(makeMemoryCard(item, byId('memoryStatus').value === 'deleted'));
}

function makeMemoryCard(item, deleted = false) {
  const card = document.createElement('article'); card.className = 'memory-card';
  const top = document.createElement('div'); top.className = 'memory-card-top'; top.append(categoryTag(item.category));
  const priority = document.createElement('span'); priority.className = 'priority'; priority.textContent = `PRIORITY ${item.priority}/5`; top.append(priority);
  const content = document.createElement('p'); content.className = 'memory-card-content'; content.textContent = item.content;
  const reason = document.createElement('p'); reason.className = 'memory-card-reason'; reason.textContent = item.reason || 'No retention note available.';
  const foot = document.createElement('div'); foot.className = 'memory-card-foot';
  const date = document.createElement('time'); date.className = 'memory-date'; date.dateTime = item.updated_at || item.created_at || ''; date.textContent = `${deleted ? 'DELETED' : 'UPDATED'} · ${dateLabel(deleted ? item.deleted_at : item.updated_at || item.created_at)}`; foot.append(date);
  const actions = document.createElement('div'); actions.className = 'memory-actions';
  if (deleted) {
    actions.append(actionButton('Recover', 'success-text', () => recoverMemory(item.id)));
    actions.append(actionButton('Erase', 'danger-text', () => permanentDelete(item.id)));
  } else {
    actions.append(actionButton('Edit', '', () => openMemoryDialog(item)));
    actions.append(actionButton('History', '', () => showHistory(item.id)));
    actions.append(actionButton('Delete', 'danger-text', () => softDelete(item.id)));
  }
  foot.append(actions); card.append(top, content, reason, foot); return card;
}

function actionButton(label, className, callback) {
  const button = document.createElement('button'); button.type = 'button'; button.className = `mini-button ${className}`; button.textContent = label; button.addEventListener('click', callback); return button;
}

let memorySearchTimer;
byId('memorySearch').addEventListener('input', () => {
  window.clearTimeout(memorySearchTimer); memorySearchTimer = window.setTimeout(loadMemories, 180);
});
byId('memoryCategory').addEventListener('change', renderMemoryList);
byId('memorySort').addEventListener('change', renderMemoryList);
byId('memoryStatus').addEventListener('change', loadMemories);
byId('addMemory').addEventListener('click', () => openMemoryDialog());
byId('addMemoryTop').addEventListener('click', () => { navigate('memories'); openMemoryDialog(); });

function openMemoryDialog(item = null) {
  byId('memoryDialogTitle').textContent = item ? 'Edit a memory' : 'Add a memory';
  byId('editMemoryId').value = item?.id || '';
  byId('memoryContent').value = item?.content || '';
  byId('memoryCategoryInput').value = item?.category || 'fact';
  byId('memoryPriority').value = String(item?.priority || 3);
  byId('memoryFormMessage').textContent = '';
  byId('saveMemory').textContent = item ? 'Save version' : 'Save memory';
  byId('memoryDialog').showModal();
  byId('memoryContent').focus();
}
document.querySelectorAll('.close-dialog').forEach((button) => button.addEventListener('click', (event) => {
  event.preventDefault(); button.closest('dialog').close();
}));

byId('memoryForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const content = byId('memoryContent').value.trim();
  if (!content) { byId('memoryFormMessage').textContent = 'Please enter memory content.'; return; }
  const id = byId('editMemoryId').value;
  const body = JSON.stringify({ content, category: byId('memoryCategoryInput').value, priority: Number(byId('memoryPriority').value) });
  byId('saveMemory').disabled = true;
  try {
    if (id) await api(`/memories/${id}`, { method: 'PATCH', body });
    else await api('/memories', { method: 'POST', body });
    byId('memoryDialog').close(); toast(id ? 'Memory updated. A new version was recorded.' : 'Memory saved.');
    await Promise.all([loadMemories(), loadUniverse(), loadTimeline(), loadAnalytics()]);
  } catch (error) { byId('memoryFormMessage').textContent = error.message; }
  finally { byId('saveMemory').disabled = false; }
});

function openAction({ title, description, destructive = false, requirePhrase = false, confirmLabel = 'Confirm', run }) {
  actionHandler = run;
  byId('actionDialogTitle').textContent = title;
  byId('actionDescription').textContent = description;
  byId('confirmationLabel').hidden = !requirePhrase;
  byId('confirmationInput').hidden = !requirePhrase;
  byId('confirmationInput').value = '';
  byId('confirmAction').textContent = confirmLabel;
  byId('confirmAction').classList.toggle('danger', destructive);
  byId('actionDialog').showModal();
}
byId('actionForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (byId('actionDialog').returnValue === 'cancel') { byId('actionDialog').close(); return; }
  if (byId('confirmationLabel').hidden === false && byId('confirmationInput').value !== 'PERMANENTLY DELETE') {
    toast('Enter the exact confirmation phrase to continue.', 'error'); return;
  }
  const handler = actionHandler; actionHandler = null;
  byId('actionDialog').close();
  if (handler) await handler();
});

async function refreshData() {
  await Promise.all([loadMemories(), loadUniverse(), loadTimeline(), loadAnalytics()]);
}
async function softDelete(id) {
  openAction({ title: 'Move to deleted memories?', description: 'This memory will be removed from active retrieval. You can recover it later from the deleted view.', destructive: true, confirmLabel: 'Move to deleted', run: async () => {
    try { await api(`/memories/${id}`, { method: 'DELETE' }); toast('Memory moved to deleted.'); await refreshData(); }
    catch (error) { toast(error.message, 'error'); }
  }});
}
async function recoverMemory(id) {
  try { await api(`/memories/${id}/recover`, { method: 'POST' }); toast('Memory recovered to active memories.'); await refreshData(); }
  catch (error) { toast(error.message, 'error'); }
}
async function permanentDelete(id) {
  openAction({ title: 'Permanently erase this memory?', description: 'This removes the soft-deleted memory and its version history from this SQLite database. Backups and copies already sent to external services are not affected. This cannot be undone.', destructive: true, requirePhrase: true, confirmLabel: 'Erase permanently', run: async () => {
    try {
      await api(`/memories/${id}/permanent`, { method: 'DELETE', body: JSON.stringify({ confirmation: 'PERMANENTLY DELETE' }) });
      toast('Memory and versions were permanently erased from this database.'); await refreshData();
    } catch (error) { toast(error.message, 'error'); }
  }});
}

async function showHistory(id) {
  try {
    const versions = await api(`/memories/${id}/versions`);
    const target = byId('historyVersions'); target.replaceChildren();
    if (!versions.length) { target.textContent = 'No versions were found.'; }
    for (const version of versions) {
      const block = document.createElement('article'); block.className = 'history-version';
      const head = document.createElement('div'); head.className = 'history-version-head';
      const title = document.createElement('strong'); title.textContent = `Version ${version.version} · ${version.category} · priority ${version.priority}/5`;
      const time = document.createElement('time'); time.textContent = dateLabel(version.created_at);
      head.append(title, time);
      const content = document.createElement('p'); content.textContent = version.content;
      const reason = document.createElement('p'); reason.className = 'muted small-copy'; reason.textContent = version.reason;
      const restore = document.createElement('button'); restore.type = 'button'; restore.className = 'button quiet'; restore.textContent = 'Restore as a new version';
      restore.addEventListener('click', async () => {
        try { await api(`/memories/${id}/restore/${version.version}`, { method: 'POST' }); byId('historyDialog').close(); toast('Version restored as a new version.'); await refreshData(); }
        catch (error) { toast(error.message, 'error'); }
      });
      block.append(head, content, reason, restore); target.append(block);
    }
    byId('historyDialog').showModal();
  } catch (error) { toast(error.message, 'error'); }
}
byId('closeHistory').addEventListener('click', () => byId('historyDialog').close());

const stopWords = new Set('about after again also another around because before being between both could every from have into just more most other over should some such than that their them then there these they this those through under until very what when where which while with would your you yourself from'.split(' '));
function contentTerms(text) {
  return new Set(String(text || '').toLowerCase().match(/[a-z0-9]{4,}/g)?.filter((word) => !stopWords.has(word)) || []);
}
function relatedTerms(left, right) {
  const a = contentTerms(`${left.content} ${left.category}`); const b = contentTerms(`${right.content} ${right.category}`);
  return [...a].filter((word) => b.has(word)).sort();
}
function visibleUniverseItems() {
  const q = byId('universeSearch').value.trim().toLowerCase(); const category = byId('universeCategory').value;
  return universeMemories.filter((item) => (!category || item.category === category) && (!q || `${item.content} ${item.category}`.toLowerCase().includes(q)));
}
async function loadUniverse() {
  try {
    universeMemories = await api('/memories');
    updateCategorySelect(byId('universeCategory'), universeMemories);
    renderUniverse();
  } catch (error) { handleAuthError(error); }
}
function memoryCoordinates(index, count) {
  if (count === 1) return { x: 380, y: 235 };
  const angle = (Math.PI * 2 * index / count) - Math.PI / 2;
  const ring = count > 8 && index >= Math.ceil(count / 2) ? .72 : 1;
  return { x: 380 + Math.cos(angle) * 242 * ring, y: 240 + Math.sin(angle) * 153 * ring };
}
function showNodeDetail(item) {
  const target = byId('nodeDetail'); target.replaceChildren();
  const kicker = document.createElement('span'); kicker.className = 'section-kicker'; kicker.textContent = 'SELECTED MEMORY';
  const tag = document.createElement('span'); tag.className = `node-category ${categoryClass(item.category)}`; tag.textContent = item.category;
  const content = document.createElement('p'); content.className = 'node-content'; content.textContent = item.content;
  const meta = document.createElement('p'); meta.className = 'node-meta'; meta.textContent = `Priority ${item.priority}/5 · Updated ${dateLabel(item.updated_at || item.created_at)}${item.reason ? ` · ${item.reason}` : ''}`;
  const actions = document.createElement('div'); actions.className = 'node-actions';
  const edit = document.createElement('button'); edit.className = 'button quiet'; edit.textContent = 'Edit'; edit.addEventListener('click', () => openMemoryDialog(item));
  const history = document.createElement('button'); history.className = 'button quiet'; history.textContent = 'History'; history.addEventListener('click', () => showHistory(item.id));
  const open = document.createElement('button'); open.className = 'button primary'; open.textContent = 'Open in archive'; open.addEventListener('click', () => { byId('memoryStatus').value = 'active'; byId('memorySearch').value = ''; navigate('memories'); });
  actions.append(edit, history, open); target.append(kicker, tag, content, meta, actions);
}
function renderUniverse() {
  const items = visibleUniverseItems();
  byId('universeCount').textContent = String(universeMemories.length);
  const edges = byId('graphEdges'); const nodes = byId('graphNodes'); edges.replaceChildren(); nodes.replaceChildren();
  const coords = items.map((_, index) => memoryCoordinates(index, items.length));
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      const shared = relatedTerms(items[i], items[j]);
      if (shared.length < 2) continue;
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', coords[i].x); line.setAttribute('y1', coords[i].y); line.setAttribute('x2', coords[j].x); line.setAttribute('y2', coords[j].y); line.setAttribute('class', 'graph-edge'); line.setAttribute('aria-label', `Shared terms: ${shared.slice(0, 5).join(', ')}`);
      edges.append(line);
    }
  }
  items.forEach((item, index) => {
    const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    group.setAttribute('class', `memory-node ${categoryClass(item.category)}`); group.setAttribute('transform', `translate(${coords[index].x} ${coords[index].y})`); group.setAttribute('tabindex', '0'); group.setAttribute('role', 'button'); group.setAttribute('aria-label', `${item.category}: ${item.content}`);
    group.dataset.memoryId = item.id;
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle'); circle.setAttribute('r', String(Math.min(43, Math.max(25, 22 + item.content.length / 20))));
    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text'); label.textContent = String(index + 1);
    group.append(circle, label); group.addEventListener('click', () => selectUniverseMemory(item)); group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectUniverseMemory(item); } }); nodes.append(group);
  });
  const list = byId('universeList'); list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = universeMemories.length ? 'No memories match the current search and category.' : 'Your map is ready for its first memory. Save a preference or add one from All Memories.';
    list.append(empty);
    const detail = byId('nodeDetail'); detail.replaceChildren();
    const kicker = document.createElement('span'); kicker.className = 'section-kicker'; kicker.textContent = 'SELECTED MEMORY';
    const message = document.createElement('div'); message.className = 'detail-empty'; message.textContent = universeMemories.length ? 'No result selected.' : 'No saved memories yet.'; detail.append(kicker, message);
  } else {
    for (const [index, item] of items.entries()) {
      const button = document.createElement('button'); button.className = 'universe-item'; button.type = 'button'; button.textContent = `${index + 1}. ${item.content}`; button.title = `${item.category} · priority ${item.priority}/5`; button.dataset.memoryId = item.id;
      button.addEventListener('click', () => selectUniverseMemory(item)); list.append(button);
    }
    if (items.length === 1) selectUniverseMemory(items[0]);
    else {
      byId('nodeDetail').replaceChildren(); const kicker = document.createElement('span'); kicker.className = 'section-kicker'; kicker.textContent = 'SELECTED MEMORY'; const empty = document.createElement('div'); empty.className = 'detail-empty'; const mark = document.createElement('span'); mark.textContent = '◎'; const text = document.createElement('p'); text.textContent = 'Select a point or a memory in the accessible list to inspect its details.'; empty.append(mark, text); byId('nodeDetail').append(kicker, empty);
    }
  }
}
function selectUniverseMemory(item) {
  document.querySelectorAll('.memory-node').forEach((node) => node.classList.toggle('selected', Number(node.dataset.memoryId) === Number(item.id)));
  document.querySelectorAll('.universe-item').forEach((node) => node.classList.toggle('selected', Number(node.dataset.memoryId) === Number(item.id)));
  showNodeDetail(item);
}
byId('universeSearch').addEventListener('input', renderUniverse);
byId('universeCategory').addEventListener('change', renderUniverse);

async function loadTimeline() {
  const target = byId('timelineList'); target.replaceChildren();
  const loading = document.createElement('p'); loading.className = 'muted'; loading.textContent = 'Loading recorded events…'; target.append(loading);
  try {
    const records = await api('/timeline?limit=150'); target.replaceChildren();
    records.sort((a, b) => timestampValue(b.created_at) - timestampValue(a.created_at));
    if (!records.length) { const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = 'No recorded memory changes yet. New memories and version changes will appear here.'; target.append(empty); return; }
    for (const entry of records) {
      const row = document.createElement('article'); row.className = `timeline-item event-${entry.event_type}`;
      const time = document.createElement('time'); time.className = 'timeline-date'; time.dateTime = entry.created_at || ''; time.textContent = dateLabel(entry.created_at);
      const title = document.createElement('h3'); title.className = 'timeline-title';
      const label = { created: 'Memory created', updated: `Memory updated · version ${entry.version}`, restored: `Version restored${entry.version ? ` · version ${entry.version}` : ''}`, deleted: 'Memory moved to deleted', recovered: 'Memory recovered', permanently_deleted: 'Memory permanently erased' }[entry.event_type] || 'Memory event';
      title.textContent = label;
      const content = document.createElement('p'); content.className = 'timeline-content'; content.textContent = entry.content || (entry.event_type === 'permanently_deleted' ? `Memory #${entry.memory_id}; content and versions were erased from this database.` : `Memory #${entry.memory_id}${entry.category ? ` · ${entry.category}` : ''}`);
      const meta = document.createElement('div'); meta.className = 'timeline-meta'; meta.textContent = entry.reason || `Memory #${entry.memory_id}${entry.category ? ` · ${entry.category}` : ''}`;
      row.append(time, title, content, meta); target.append(row);
    }
  } catch (error) { target.replaceChildren(); const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = `Timeline unavailable: ${error.message}`; target.append(empty); }
}
byId('refreshTimeline').addEventListener('click', loadTimeline);

async function loadAnalytics() {
  const target = byId('analyticsGrid'); target.replaceChildren(); const loading = document.createElement('div'); loading.className = 'panel loading-card'; loading.textContent = 'Loading account metrics…'; target.append(loading);
  try {
    const data = await api('/analytics'); target.replaceChildren();
    const stats = [
      ['Active memories', data.active_memories, 'Currently available for retrieval'],
      ['Deleted memories', data.deleted_memories, 'Soft-deleted and recoverable'],
      ['Recorded changes', data.recorded_lifecycle_events, 'Delete, recover, and restore events'],
      ['Version records', data.memory_versions, 'Preserved snapshots in memory history']
    ];
    for (const [label, value, description] of stats) {
      const card = document.createElement('article'); card.className = 'stat-card'; const name = document.createElement('span'); name.textContent = label; const count = document.createElement('strong'); count.textContent = String(value); const note = document.createElement('small'); note.textContent = description; card.append(name, count, note); target.append(card);
    }
    const categoryPanel = document.createElement('section'); categoryPanel.className = 'category-chart'; const chartTitle = document.createElement('h2'); chartTitle.textContent = 'Category mix'; const chartIntro = document.createElement('p'); chartIntro.textContent = 'Active memory records grouped by their saved category.'; categoryPanel.append(chartTitle, chartIntro);
    if (!data.categories.length) { const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = 'Category totals will appear after you save a memory.'; categoryPanel.append(empty); }
    const max = Math.max(1, ...data.categories.map((item) => item.count));
    for (const item of data.categories) {
      const row = document.createElement('div'); row.className = 'chart-row'; const label = document.createElement('span'); label.textContent = item.category; const track = document.createElement('div'); track.className = 'chart-track'; const fill = document.createElement('div'); fill.className = `chart-fill ${categoryClass(item.category)}`; fill.style.width = `${(item.count / max) * 100}%`; fill.setAttribute('aria-label', `${item.count} ${item.category} memories`); track.append(fill); const count = document.createElement('span'); count.textContent = String(item.count); row.append(label, track, count); categoryPanel.append(row);
    }
    target.append(categoryPanel);
    const activity = document.createElement('section'); activity.className = 'activity-card'; const title = document.createElement('h2'); title.textContent = 'Recorded history'; const intro = document.createElement('p'); intro.textContent = 'Persisted account records, not inferred activity.'; const pair = document.createElement('div'); pair.className = 'activity-pair';
    for (const [number, label] of [[data.memory_versions, 'memory versions'], [data.recorded_lifecycle_events, 'lifecycle actions']]) { const block = document.createElement('div'); const amount = document.createElement('strong'); amount.textContent = String(number); const caption = document.createElement('span'); caption.textContent = label; block.append(amount, caption); pair.append(block); }
    activity.append(title, intro, pair); target.append(activity);
  } catch (error) { target.replaceChildren(); const empty = document.createElement('div'); empty.className = 'panel loading-card'; empty.textContent = `Analytics unavailable: ${error.message}`; target.append(empty); }
}

async function bootstrap() {
  try {
    const mode = await api('/session-mode');
    invitationRequired = Boolean(mode.invitation_required);
    byId('invitationField').hidden = !invitationRequired;
    byId('invitationCode').required = invitationRequired;
    if (mode.cookie_auth) {
      localStorage.removeItem('sessionToken');
      token = null;
      await api('/session');
      await showWorkspace();
      return;
    }
    if (token) await showWorkspace();
  } catch (error) {
    if (token) handleAuthError(error);
  }
}
bootstrap();
