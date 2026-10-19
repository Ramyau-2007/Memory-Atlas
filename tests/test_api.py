import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import app.db as database
from app.agent import ModelUnavailableError
from app.main import app
from app.security import model_usage_guard, request_limiter
from app.invitations import create_invitation, disable_account, enable_account


class ApiWorkflowTests(unittest.TestCase):
    def setUp(self):
        request_limiter.clear()
        model_usage_guard.clear()
        self.demo_mode_patch = patch.dict(os.environ, {'DEMO_INVITATIONS_REQUIRED':'false'})
        self.demo_mode_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / 'workflow.sqlite3')
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.alice = self._register_and_login('alice')
        self.bob = self._register_and_login('bob')

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        database.DATABASE_PATH = self.original_database_path
        self.demo_mode_patch.stop()
        self.temp.cleanup()

    def _register_and_login(self, username):
        response = self.client.post('/api/register', json={
            'username': username, 'password': 'a-long-test-password'
        })
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post('/api/login', json={
            'username': username, 'password': 'a-long-test-password'
        })
        self.assertEqual(response.status_code, 200, response.text)
        return {'Authorization': 'Bearer ' + response.json()['token']}

    def _chat_without_provider(self, headers, payload):
        adapter = SimpleNamespace(
            provider='deterministic', model_name='Deterministic development adapter',
            answer=lambda message, memories, recent: f'Development reply to: {message}',
            groq_messages=lambda *args: [], ollama_prompt=lambda *args: ''
        )
        with patch('app.main.ModelAdapter', return_value=adapter):
            return self.client.post('/api/chat', headers=headers, json=payload)

    def test_conversation_history_titles_isolation_and_delete_safety(self):
        self.assertEqual(self.client.get('/api/conversations').status_code, 401)
        created_a = self.client.post('/api/conversations', headers=self.alice, json={})
        created_b = self.client.post('/api/conversations', headers=self.alice, json={})
        self.assertEqual(created_a.status_code, 200, created_a.text)
        self.assertEqual(created_b.status_code, 200, created_b.text)
        first_id, second_id = created_a.json()['id'], created_b.json()['id']
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.client.get('/api/conversations', headers=self.bob).json(), [])
        self.assertEqual(self.client.get(f'/api/conversations/{first_id}/messages', headers=self.bob).status_code, 404)

        first_chat = self._chat_without_provider(self.alice, {
            'message': 'I prefer short Python examples please', 'conversation_id': first_id
        })
        second_chat = self._chat_without_provider(self.alice, {
            'message': 'Help me plan a garden', 'conversation_id': second_id
        })
        self.assertEqual(first_chat.status_code, 200, first_chat.text)
        self.assertEqual(second_chat.status_code, 200, second_chat.text)
        first_messages = self.client.get(f'/api/conversations/{first_id}/messages', headers=self.alice).json()
        second_messages = self.client.get(f'/api/conversations/{second_id}/messages', headers=self.alice).json()
        self.assertEqual([row['role'] for row in first_messages], ['user', 'assistant'])
        self.assertEqual(first_messages[0]['content'], 'I prefer short Python examples please')
        self.assertEqual([row['content'] for row in second_messages if row['role'] == 'user'], ['Help me plan a garden'])

        listing = self.client.get('/api/conversations', headers=self.alice).json()
        self.assertEqual(next(row['title'] for row in listing if row['id'] == first_id), 'I prefer short Python examples please')
        self.assertEqual(self.client.patch(f'/api/conversations/{first_id}', headers=self.bob, json={'title': 'Hijack'}).status_code, 404)
        renamed = self.client.patch(f'/api/conversations/{first_id}', headers=self.alice, json={'title': 'Python notes'})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(self.client.get(f'/api/conversations/{first_id}/messages', headers=self.alice).json(), first_messages)

        memory_id = self.client.post('/api/memories', headers=self.alice, json={
            'content': 'I prefer concise Python examples across projects', 'category': 'preference', 'priority': 4
        }).json()['id']
        cross_chat = self._chat_without_provider(self.alice, {
            'message': 'Show me Python examples', 'conversation_id': second_id
        })
        self.assertEqual(cross_chat.status_code, 200, cross_chat.text)
        self.assertIn(memory_id, [row['id'] for row in cross_chat.json()['retrieved']])

        self.assertEqual(self.client.delete(f'/api/conversations/{first_id}', headers=self.bob).status_code, 404)
        removed = self.client.delete(f'/api/conversations/{first_id}', headers=self.alice)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.client.get(f'/api/conversations/{first_id}/messages', headers=self.alice).status_code, 404)
        self.assertIn(memory_id, [row['id'] for row in self.client.get('/api/memories', headers=self.alice).json()])

    def test_edit_regenerates_and_prunes_only_later_branch_and_soft_deletes_its_auto_memory(self):
        initial=self._chat_without_provider(self.alice,{'message':'I prefer Python examples for learning'})
        self.assertEqual(initial.status_code,200,initial.text)
        cid=initial.json()['conversation_id']; original_id=initial.json()['message_id']
        original_memory=self.client.get('/api/memories',headers=self.alice).json()[0]
        self.assertEqual(original_memory['source_message_id'],original_id)
        later=self._chat_without_provider(self.alice,{'message':'Explain Python lists','conversation_id':cid})
        self.assertEqual(later.status_code,200,later.text)
        adapter=SimpleNamespace(provider='deterministic',model_name='mock-deterministic',
            answer=lambda message,memories,recent: f'Regenerated for: {message}; prior={len(recent)}',
            groq_messages=lambda *args:[],ollama_prompt=lambda *args:'')
        with patch('app.main.ModelAdapter',return_value=adapter):
            edited=self.client.patch(f'/api/conversations/{cid}/messages/{original_id}',headers=self.alice,
                                     json={'message':'Help me plan a garden instead'})
        self.assertEqual(edited.status_code,200,edited.text)
        data=edited.json(); self.assertTrue(data['edited']); self.assertEqual(data['removed_later_messages'],3)
        self.assertEqual(data['answer'],'Regenerated for: Help me plan a garden instead; prior=0')
        messages=self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json()
        self.assertEqual([row['role'] for row in messages],['user','assistant'])
        self.assertEqual(messages[0]['content'],'Help me plan a garden instead')
        self.assertEqual(messages[1]['content'],data['answer'])
        self.assertEqual(self.client.get('/api/memories',headers=self.alice).json(),[])
        deleted=self.client.get('/api/memories/deleted',headers=self.alice).json()
        self.assertEqual([row['id'] for row in deleted],[original_memory['id']])
        self.assertEqual(len(self.client.get(f"/api/memories/{original_memory['id']}/versions",headers=self.alice).json()),1)
        self.assertIn(original_memory['id'],data['deactivated_memories'])

    def test_edit_preserves_user_versioned_memory_and_checks_ownership(self):
        chat=self._chat_without_provider(self.alice,{'message':'I prefer Python examples'})
        cid=chat.json()['conversation_id']; mid=chat.json()['message_id']
        memory_id=self.client.get('/api/memories',headers=self.alice).json()[0]['id']
        self.client.patch(f'/api/memories/{memory_id}',headers=self.alice,json={
            'content':'I prefer short Python examples','category':'preference','priority':4})
        self.assertEqual(self.client.patch(f'/api/conversations/{cid}/messages/{mid}',headers=self.bob,
            json={'message':'Changed by Bob'}).status_code,404)
        self.assertEqual(self.client.patch(f'/api/conversations/{cid}/messages/{mid}',json={
            'message':'Unauthenticated change'}).status_code,401)
        edited=self._chat_edit_without_provider(cid,mid,'I prefer gardening examples')
        self.assertEqual(edited.status_code,200,edited.text)
        self.assertIn(memory_id,edited.json()['preserved_user_edited_memories'])
        active=self.client.get('/api/memories',headers=self.alice).json()
        self.assertEqual(next(row for row in active if row['id']==memory_id)['content'],'I prefer short Python examples')
        self.assertEqual(len(self.client.get(f'/api/memories/{memory_id}/versions',headers=self.alice).json()),2)

    def _chat_edit_without_provider(self,cid,mid,message):
        adapter=SimpleNamespace(provider='deterministic',model_name='mock-deterministic',
            answer=lambda text,memories,recent:'Regenerated safely',groq_messages=lambda *args:[],ollama_prompt=lambda *args:'')
        with patch('app.main.ModelAdapter',return_value=adapter):
            return self.client.patch(f'/api/conversations/{cid}/messages/{mid}',headers=self.alice,json={'message':message})

    def test_edit_provider_failure_keeps_message_branch_and_memories_unchanged(self):
        chat=self._chat_without_provider(self.alice,{'message':'I prefer Python examples'})
        cid=chat.json()['conversation_id']; mid=chat.json()['message_id']
        self._chat_without_provider(self.alice,{'message':'A later follow-up','conversation_id':cid})
        before=self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json()
        memories=self.client.get('/api/memories',headers=self.alice).json()
        with patch('app.main.generate_chat_result',side_effect=ModelUnavailableError('Mock model unavailable')):
            failed=self.client.patch(f'/api/conversations/{cid}/messages/{mid}',headers=self.alice,
                                     json={'message':'I prefer gardening'})
        self.assertEqual(failed.status_code,503)
        self.assertEqual(self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json(),before)
        self.assertEqual(self.client.get('/api/memories',headers=self.alice).json(),memories)

    def test_edit_shares_chat_throttle_and_rejected_edit_changes_nothing(self):
        chat=self._chat_without_provider(self.alice,{'message':'I prefer Python examples'})
        cid=chat.json()['conversation_id']; mid=chat.json()['message_id']
        request_limiter.clear()
        with patch.dict(os.environ,{'CHAT_RATE_LIMIT':'1','CHAT_RATE_WINDOW_SECONDS':'600'}):
            allowed=self._chat_edit_without_provider(cid,mid,'I prefer concise Python examples')
            self.assertEqual(allowed.status_code,200,allowed.text)
            messages=self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json()
            memories=self.client.get('/api/memories',headers=self.alice).json()
            versions={item['id']:self.client.get(f"/api/memories/{item['id']}/versions",headers=self.alice).json()
                      for item in memories}
            with patch('app.main.generate_chat_result') as generate:
                rejected=self.client.patch(f'/api/conversations/{cid}/messages/{mid}',headers=self.alice,
                                           json={'message':'I prefer gardening examples'})
            generate.assert_not_called()
        self.assertEqual(rejected.status_code,429,rejected.text)
        self.assertIn('Retry-After',rejected.headers)
        self.assertEqual(self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json(),messages)
        self.assertEqual(self.client.get('/api/memories',headers=self.alice).json(),memories)
        for memory_id,history in versions.items():
            self.assertEqual(self.client.get(f'/api/memories/{memory_id}/versions',headers=self.alice).json(),history)

    def test_edit_exhausted_groq_quota_returns_429_without_provider_call_or_branch_changes(self):
        chat=self._chat_without_provider(self.alice,{'message':'I prefer Python examples'})
        cid=chat.json()['conversation_id']; mid=chat.json()['message_id']
        self._chat_without_provider(self.alice,{'message':'A later follow-up','conversation_id':cid})
        messages=self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json()
        memories=self.client.get('/api/memories',headers=self.alice).json()
        deleted=self.client.get('/api/memories/deleted',headers=self.alice).json()
        versions={item['id']:self.client.get(f"/api/memories/{item['id']}/versions",headers=self.alice).json()
                  for item in memories+deleted}
        with database.connect(self.db_path) as db:
            user_id=db.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
        with patch.dict(os.environ,{
            'MODEL_PROVIDER':'groq','GROQ_API_KEY':'test-only-key',
            'MODEL_ACTIONS_PER_USER_PER_DAY':'1','MODEL_ACTIONS_PER_DAY':'1',
        },clear=False):
            model_usage_guard.clear()
            model_usage_guard.reserve(user_id)
            with patch('app.agent.requests.post') as post:
                rejected=self.client.patch(f'/api/conversations/{cid}/messages/{mid}',headers=self.alice,
                                           json={'message':'I prefer gardening examples'})
        self.assertEqual(rejected.status_code,429,rejected.text)
        self.assertIn('Retry-After',rejected.headers)
        post.assert_not_called()
        self.assertEqual(self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json(),messages)
        self.assertEqual(self.client.get('/api/memories',headers=self.alice).json(),memories)
        self.assertEqual(self.client.get('/api/memories/deleted',headers=self.alice).json(),deleted)
        for memory_id,history in versions.items():
            self.assertEqual(self.client.get(f'/api/memories/{memory_id}/versions',headers=self.alice).json(),history)

    def test_edit_rejects_assistant_messages_and_invalid_or_missing_targets(self):
        chat=self._chat_without_provider(self.alice,{'message':'Hello there'})
        cid=chat.json()['conversation_id']; message_id=chat.json()['message_id']
        assistant=self.client.get(f'/api/conversations/{cid}/messages',headers=self.alice).json()[1]
        self.assertEqual(self.client.patch(f"/api/conversations/{cid}/messages/{assistant['id']}",headers=self.alice,
            json={'message':'change'}).status_code,400)
        self.assertEqual(self.client.patch(f'/api/conversations/{cid}/messages/99999',headers=self.alice,
            json={'message':'change'}).status_code,404)
        self.assertEqual(self.client.patch(f'/api/conversations/{cid}/messages/{message_id}',headers=self.alice,
            json={'message':'   '}).status_code,422)

    def test_groq_assisted_memory_creation_and_paraphrase_retrieval_across_conversations(self):
        calls = []
        def analyze(message):
            if 'fav prog' in message.lower():
                return {'message_type': 'assertion', 'about_user': True, 'confidence': 0.97,
                        'candidate': {'content': "The user's favorite programming language is Python.",
                                      'category': 'preference', 'polarity': 'positive'},
                        'search_terms': ['favorite programming language', 'Python']}
            return {'message_type': 'question', 'about_user': True, 'confidence': 0.95,
                    'candidate': None, 'search_terms': ['favorite programming language', 'Python']}
        adapter = SimpleNamespace(
            provider='groq', model_name='mock-groq', analyze_memory_message=analyze,
            groq_messages=lambda message, memories, recent: calls.append((message, memories)) or [{'role':'user','content':'mock'}],
            answer=lambda message, memories, recent: 'Mocked answer using supplied context.',
            ollama_prompt=lambda *args: '',
        )
        with patch('app.main.ModelAdapter', return_value=adapter):
            first = self.client.post('/api/chat', headers=self.alice, json={
                'message': 'my fav prog lang is python'
            })
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()['memory_analysis']['status'], 'interpreted')
            self.assertTrue(first.json()['memory_decision']['retain'])
            memory_id = first.json()['memory_decision']
            second = self.client.post('/api/chat', headers=self.alice, json={
                'message': 'u remember which lang i like?'
            })
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()['memory_analysis']['status'], 'interpreted')
            self.assertEqual(second.json()['memory_decision']['retain'], False)
            self.assertEqual(second.json()['context']['memories_supplied'][0]['content'],
                             "The user's favorite programming language is Python.")
            self.assertTrue(calls[-1][1], 'Retrieved memory must reach the Groq prompt builder.')
            self.assertEqual(self.client.get('/api/memories', headers=self.bob).json(), [])

    def test_groq_analysis_failure_uses_local_fallback_and_keeps_chat_available(self):
        adapter = SimpleNamespace(
            provider='groq', model_name='mock-groq',
            analyze_memory_message=Mock(side_effect=ModelUnavailableError('Could not connect to Groq.')),
            groq_messages=lambda *args: [{'role':'user','content':'mock'}],
            answer=lambda *args: 'Mocked answer.', ollama_prompt=lambda *args: '',
        )
        with patch('app.main.ModelAdapter', return_value=adapter):
            response = self.client.post('/api/chat', headers=self.alice, json={
                'message': 'my fav prog lang is python'
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['memory_analysis']['status'], 'provider_error_fallback')
        self.assertTrue(response.json()['memory_decision']['retain'])
        self.assertIn('my fav prog lang is python', [row['content'] for row in self.client.get('/api/memories', headers=self.alice).json()])

    def test_chat_without_existing_conversation_generates_short_title(self):
        message = 'Please help me create a practical weekly study plan for algorithms and Python programming'
        result = self._chat_without_provider(self.alice, {'message': message})
        self.assertEqual(result.status_code, 200, result.text)
        conversation_id = result.json()['conversation_id']
        row = next(item for item in self.client.get('/api/conversations', headers=self.alice).json()
                   if item['id'] == conversation_id)
        self.assertLessEqual(len(row['title']), 48)
        self.assertTrue(row['title'].startswith('Please help me create'))

    def test_auth_validation_and_ownership_failures(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        page = self.client.get('/').text
        self.assertIn('Memory Universe', page)
        self.assertIn('Memory Timeline', page)
        self.assertIn('Context inspector', page)
        self.assertIn('id="memoryGraph"', page)
        self.assertIn('id="analyticsGrid"', page)
        self.assertIn('id="newChat"', page)
        self.assertIn('id="conversationList"', page)
        script = self.client.get('/static/app.js')
        self.assertEqual(script.status_code, 200)
        self.assertIn('permanentDelete', script.text)
        self.assertIn("api('/conversations')", script.text)
        self.assertIn("/messages`)", script.text)
        self.assertIn("const welcomeState = byId('welcomeState');", script.text)
        self.assertNotIn("byId('welcomeState').hidden", script.text)
        self.assertIn('async function createConversation()', script.text)
        self.assertIn("await api('/conversations', { method: 'POST'", script.text)
        self.assertIn('recoverMemory', script.text)
        self.assertIn("api('/timeline?limit=150')", script.text)
        self.assertIn("api('/analytics')", script.text)
        stylesheet = self.client.get('/static/style.css')
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn('--bg: #11152E', stylesheet.text)
        self.assertIn('--violet: #9B7BFF', stylesheet.text)
        self.assertIn('--teal: #32D6C0', stylesheet.text)
        self.assertNotIn('#20201e', stylesheet.text.lower())
        self.assertIn('id="memorySort"', page)
        self.assertIn("byId('memorySort').addEventListener", script.text)
        self.assertEqual(self.client.get('/api/memories').status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={
            'username': 'alice', 'password': 'incorrect-password'
        }).status_code, 401)
        self.assertEqual(self.client.post('/api/register', json={
            'username': 'short', 'password': 'tiny'
        }).status_code, 422)

        memory_id = self.client.post('/api/memories', headers=self.alice, json={
            'content': 'Alice private memory', 'category': 'fact', 'priority': 2
        }).json()['id']
        self.assertEqual(self.client.patch(f'/api/memories/{memory_id}', headers=self.bob, json={
            'content': 'stolen edit', 'category': 'fact', 'priority': 2
        }).status_code, 404)
        self.assertEqual(self.client.get(f'/api/memories/{memory_id}/versions', headers=self.bob).status_code, 404)
        self.assertEqual(self.client.post(f'/api/memories/{memory_id}/restore/1', headers=self.bob).status_code, 404)
        self.assertEqual(self.client.delete(f'/api/memories/{memory_id}', headers=self.bob).status_code, 404)
        self.assertEqual(self.client.post(f'/api/memories/{memory_id}/recover', headers=self.bob).status_code, 404)
        self.assertEqual(self.client.request('DELETE', f'/api/memories/{memory_id}/permanent', headers=self.bob, json={
            'confirmation': 'PERMANENTLY DELETE'
        }).status_code, 404)
        self.assertEqual(self.client.get('/api/memories', headers=self.bob).json(), [])

    def test_logout_revokes_token(self):
        result = self.client.post('/api/logout', headers=self.alice)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get('/api/memories', headers=self.alice).status_code, 401)

    def test_model_failure_returns_clear_503_without_fallback_text(self):
        with patch('app.main.ModelAdapter.answer', side_effect=ModelUnavailableError(
            'Could not connect to Ollama. Start Ollama and retry.'
        )):
            response = self.client.post('/api/chat', headers=self.alice, json={
                'message': 'Test model availability'
            })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'Could not connect to Ollama. Start Ollama and retry.')
        self.assertNotIn('Development mode', response.text)

    def test_markdown_vendor_assets_are_served_locally(self):
        marked=self.client.get('/static/vendor/marked-18.0.14.min.js')
        purifier=self.client.get('/static/vendor/dompurify-3.4.17.min.js')
        self.assertEqual(marked.status_code,200)
        self.assertEqual(purifier.status_code,200)
        self.assertIn('javascript',marked.headers.get('content-type',''))
        self.assertIn('javascript',purifier.headers.get('content-type',''))
        self.assertIn('marked',marked.text[:200].lower())
        self.assertIn('dompurify',purifier.text[:300].lower())

    def test_groq_chat_returns_source_and_context_without_exposing_api_key(self):
        self.client.post('/api/memories', headers=self.alice, json={
            'content': 'I prefer concise Python examples', 'category': 'preference', 'priority': 4
        })
        provider_response = type('Response', (), {
            'raise_for_status': lambda self: None,
            'json': lambda self: {'choices': [{'message': {'content': 'Mocked hosted model response.'}}]},
        })()
        with patch.dict('os.environ', {
            'MODEL_PROVIDER': 'groq', 'GROQ_API_KEY': 'test-only-key',
            'GROQ_MODEL': 'openai/gpt-oss-20b',
        }, clear=True), patch('app.agent.requests.post', return_value=provider_response) as post:
            response = self.client.post('/api/chat', headers=self.alice, json={
                'message': 'Can you give me Python examples?'
            })

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['provider'], 'groq')
        self.assertEqual(data['model'], 'openai/gpt-oss-20b')
        self.assertEqual(data['answer'], 'Mocked hosted model response.')
        self.assertEqual(data['context']['memories_supplied'][0]['content'], 'I prefer concise Python examples')
        self.assertEqual(len(post.call_args.kwargs['json']['messages']), 2)
        self.assertNotIn('test-only-key', response.text)

    def test_chat_retrieval_crud_versions_recovery_and_permanent_delete(self):
        created = self.client.post('/api/memories', headers=self.alice, json={
            'content': 'I prefer Python examples', 'category': 'preference', 'priority': 3
        })
        self.assertEqual(created.status_code, 200)
        memory_id = created.json()['id']
        self.assertEqual(self.client.request('DELETE', f'/api/memories/{memory_id}/permanent', headers=self.alice, json={
            'confirmation': 'PERMANENTLY DELETE'
        }).status_code, 404, 'Permanent deletion must require the soft-deleted state.')

        chat = self._chat_without_provider(self.alice, {'message': 'Can you show Python examples?'})
        self.assertEqual(chat.status_code, 200, chat.text)
        self.assertEqual(chat.json()['provider'], 'deterministic')
        self.assertEqual(chat.json()['retrieved'][0]['id'], memory_id)
        conversation_id = chat.json()['conversation_id']
        self.assertEqual(self.client.post('/api/chat', headers=self.bob, json={
            'message': 'Access someone else conversation', 'conversation_id': conversation_id
        }).status_code, 404)

        edited = self.client.patch(f'/api/memories/{memory_id}', headers=self.alice, json={
            'content': 'I prefer concise Python examples', 'category': 'preference', 'priority': 4
        })
        self.assertEqual(edited.status_code, 200, edited.text)
        history = self.client.get(f'/api/memories/{memory_id}/versions', headers=self.alice)
        self.assertEqual([item['version'] for item in history.json()], [2, 1])
        restored = self.client.post(f'/api/memories/{memory_id}/restore/1', headers=self.alice)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(len(self.client.get(f'/api/memories/{memory_id}/versions', headers=self.alice).json()), 3)

        self.assertEqual(self.client.delete(f'/api/memories/{memory_id}', headers=self.alice).json()['deleted'], 'soft')
        self.assertEqual(self.client.get('/api/memories', headers=self.alice).json(), [])
        deleted = self.client.get('/api/memories/deleted', headers=self.alice)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()[0]['id'], memory_id)
        self.assertEqual(self.client.request('DELETE', f'/api/memories/{memory_id}/permanent', headers=self.alice, json={
            'confirmation': 'wrong'
        }).status_code, 400)
        self.assertEqual(self.client.post(f'/api/memories/{memory_id}/recover', headers=self.alice).status_code, 200)
        self.assertEqual(self.client.get('/api/memories/deleted', headers=self.alice).json(), [])
        self.client.delete(f'/api/memories/{memory_id}', headers=self.alice)
        final_delete = self.client.request('DELETE', f'/api/memories/{memory_id}/permanent', headers=self.alice, json={
            'confirmation': 'PERMANENTLY DELETE'
        })
        self.assertEqual(final_delete.status_code, 200, final_delete.text)
        self.assertEqual(self.client.get('/api/memories/deleted', headers=self.alice).json(), [])
        self.assertEqual(self.client.get(f'/api/memories/{memory_id}/versions', headers=self.alice).status_code, 404)
        deletion_events = [event for event in self.client.get('/api/timeline', headers=self.alice).json() if event['event_type'] == 'permanently_deleted']
        self.assertEqual(len(deletion_events), 1)
        self.assertIsNone(deletion_events[0].get('content'))
        with database.connect(self.db_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_versions WHERE memory_id=?', (memory_id,)).fetchone()[0], 0)

    def test_memory_and_session_persist_after_app_restart(self):
        self.client.post('/api/memories', headers=self.alice, json={
            'content': 'Persistent note', 'category': 'fact', 'priority': 2
        })
        chat = self._chat_without_provider(self.alice, {'message': 'Keep this conversation after restart'})
        self.assertEqual(chat.status_code, 200, chat.text)
        conversation_id = chat.json()['conversation_id']
        before_restart = self.client.get(f'/api/conversations/{conversation_id}/messages', headers=self.alice).json()
        self.client_context.__exit__(None, None, None)
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.assertEqual(self.client.get('/api/memories', headers=self.alice).status_code, 200)
        self.assertEqual(self.client.get('/api/memories', headers=self.alice).json()[0]['content'], 'Persistent note')
        self.assertEqual(self.client.get(f'/api/conversations/{conversation_id}/messages', headers=self.alice).json(), before_restart)

    def test_context_timeline_and_analytics_are_real_and_user_scoped(self):
        memory_id = self.client.post('/api/memories', headers=self.alice, json={
            'content': 'I prefer Python examples for careful learning', 'category': 'preference', 'priority': 4
        }).json()['id']
        self.client.post('/api/memories', headers=self.bob, json={
            'content': 'I prefer hiking trail examples', 'category': 'preference', 'priority': 3
        })

        chat = self._chat_without_provider(self.alice, {
            'message': 'Can you give Python examples for learning?'
        })
        self.assertEqual(chat.status_code, 200, chat.text)
        payload = chat.json()
        self.assertEqual(payload['context']['query'], 'Can you give Python examples for learning?')
        self.assertEqual([item['id'] for item in payload['context']['memories_supplied']], [memory_id])
        self.assertIsNone(payload['context']['ollama_prompt'])
        self.assertIn('not an LLM', payload['context']['deterministic_note'])
        self.assertEqual(payload['provider'], 'deterministic')
        self.assertEqual(payload['model'], 'Deterministic development adapter')
        self.assertGreaterEqual(payload['timings']['retrieval_ms'], 0)
        self.assertGreaterEqual(payload['timings']['generation_ms'], 0)

        initial_timeline = self.client.get('/api/timeline', headers=self.alice).json()
        self.assertTrue(any(row['memory_id'] == memory_id and row['event_type'] == 'created' for row in initial_timeline))
        self.assertFalse(any(row.get('content') == 'I prefer hiking trail examples' for row in initial_timeline))
        alice_analytics = self.client.get('/api/analytics', headers=self.alice).json()
        bob_analytics = self.client.get('/api/analytics', headers=self.bob).json()
        self.assertEqual(alice_analytics['active_memories'], 1)
        self.assertEqual(bob_analytics['active_memories'], 1)
        self.assertEqual(alice_analytics['memory_versions'], 1)
        self.assertEqual(alice_analytics['categories'], [{'category': 'preference', 'count': 1}])

        self.assertEqual(self.client.patch(f'/api/memories/{memory_id}', headers=self.alice, json={
            'content': 'I prefer brief Python examples for careful learning', 'category': 'preference', 'priority': 4
        }).status_code, 200)
        self.assertEqual(self.client.post(f'/api/memories/{memory_id}/restore/1', headers=self.alice).status_code, 200)
        self.assertEqual(self.client.delete(f'/api/memories/{memory_id}', headers=self.alice).status_code, 200)
        self.assertEqual(self.client.post(f'/api/memories/{memory_id}/recover', headers=self.alice).status_code, 200)
        timeline = self.client.get('/api/timeline', headers=self.alice).json()
        kinds = {row['event_type'] for row in timeline}
        self.assertTrue({'created', 'updated', 'restored', 'deleted', 'recovered'}.issubset(kinds))
        self.assertEqual(self.client.get('/api/timeline', headers=self.bob).json()[0]['content'], 'I prefer hiking trail examples')
        self.assertEqual(self.client.get('/api/analytics', headers=self.alice).json()['recorded_lifecycle_events'], 3)

    def test_memory_map_data_handles_zero_one_and_multiple_records(self):
        charlie = self._register_and_login('charlie')
        self.assertEqual(self.client.get('/api/memories', headers=charlie).json(), [])
        self.client.post('/api/memories', headers=charlie, json={
            'content': 'I prefer quiet writing time', 'category': 'preference', 'priority': 3
        })
        one = self.client.get('/api/memories', headers=charlie).json()
        self.assertEqual(len(one), 1)
        self.client.post('/api/memories', headers=charlie, json={
            'content': 'My goal is to finish a writing project', 'category': 'goal', 'priority': 4
        })
        multiple = self.client.get('/api/memories', headers=charlie).json()
        self.assertEqual(len(multiple), 2)
        self.assertEqual({item['category'] for item in multiple}, {'preference', 'goal'})

    def test_request_limits_and_security_headers(self):
        request_limiter.clear()
        with patch.dict(os.environ, {'REGISTER_RATE_LIMIT':'1','REGISTER_RATE_WINDOW_SECONDS':'600'}):
            first=self.client.post('/api/register',json={'username':'new-one','password':'a-long-test-password'})
            second=self.client.post('/api/register',json={'username':'new-two','password':'a-long-test-password'})
        self.assertEqual(first.status_code,200)
        self.assertEqual(second.status_code,429)
        self.assertIn('Retry-After',second.headers)
        request_limiter.clear()
        credentials={'username':'alice','password':'a-long-test-password'}
        with patch.dict(os.environ, {'LOGIN_RATE_LIMIT':'1','LOGIN_RATE_WINDOW_SECONDS':'600'}):
            login_first=self.client.post('/api/login',json=credentials)
            login_second=self.client.post('/api/login',json=credentials)
        self.assertEqual(login_first.status_code,200)
        self.assertEqual(login_second.status_code,429)
        request_limiter.clear()
        with patch.dict(os.environ, {'CHAT_RATE_LIMIT':'1','CHAT_RATE_WINDOW_SECONDS':'600'}):
            chat_first=self._chat_without_provider(self.alice,{'message':'First limited chat.'})
            chat_second=self._chat_without_provider(self.alice,{'message':'Second limited chat.'})
        self.assertEqual(chat_first.status_code,200)
        self.assertEqual(chat_second.status_code,429)
        health=self.client.get('/api/session-mode')
        self.assertEqual(health.status_code,200)
        self.assertEqual(health.headers['x-content-type-options'],'nosniff')
        self.assertEqual(health.headers['x-frame-options'],'DENY')
        self.assertIn("script-src 'self'",health.headers['content-security-policy'])
        self.assertEqual(health.headers['cache-control'],'no-store')

    def test_request_body_limit_rejects_large_payload(self):
        with patch.dict(os.environ, {'MAX_REQUEST_BODY_BYTES':'16'}):
            response=self.client.post('/api/register',content='{"username":"alice","password":"too-large-body"}',
                                      headers={'content-type':'application/json'})
        self.assertEqual(response.status_code,413)
        self.assertIn('too large',response.json()['detail'])
        self.assertEqual(response.headers['x-content-type-options'],'nosniff')

    def test_invitation_registration_requires_valid_unexpired_single_use_code(self):
        request_limiter.clear()
        with patch.dict(os.environ, {'DEMO_INVITATIONS_REQUIRED':'true'}):
            missing = self.client.post('/api/register', json={
                'username':'invite-missing','password':'a-long-test-password'
            })
            invalid = self.client.post('/api/register', json={
                'username':'invite-invalid','password':'a-long-test-password','invitation_code':'made-up'
            })
            self.assertEqual(missing.status_code, 400)
            self.assertEqual(invalid.status_code, 400)

            expired_id, expired_code = create_invitation(1)
            with database.connect() as db:
                db.execute("UPDATE invitations SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (expired_id,))
            expired = self.client.post('/api/register', json={
                'username':'invite-expired','password':'a-long-test-password','invitation_code':expired_code
            })
            self.assertEqual(expired.status_code, 400)

            invitation_id, code = create_invitation(3)
            registered = self.client.post('/api/register', json={
                'username':'invite-success','password':'a-long-test-password','invitation_code':code
            })
            self.assertEqual(registered.status_code, 200, registered.text)
            self.assertNotIn(code, registered.text)
            reused = self.client.post('/api/register', json={
                'username':'invite-reused','password':'a-long-test-password','invitation_code':code
            })
            self.assertEqual(reused.status_code, 400)
            with database.connect() as db:
                row = db.execute('SELECT token_hash,redeemed_at FROM invitations WHERE id=?', (invitation_id,)).fetchone()
                self.assertNotEqual(row['token_hash'], code)
                self.assertTrue(row['redeemed_at'])

    def test_disabled_account_rejects_bearer_and_cookie_sessions_and_preserves_data(self):
        token = self.client.post('/api/login', json={
            'username':'alice','password':'a-long-test-password'
        }).json()['token']
        memory_response = self.client.post('/api/memories', headers=self.alice, json={
            'content':'Preserve this private preference','category':'preference','priority':3
        })
        conversation = self.client.post('/api/conversations', headers=self.alice, json={'title':'Keep this chat'}).json()
        self.assertEqual(memory_response.status_code, 200, memory_response.text)

        with patch.dict(os.environ, {'SESSION_COOKIE_AUTH':'true','SESSION_COOKIE_SECURE':'false'}):
            cookie_client = TestClient(app)
            cookie_login = cookie_client.post('/api/login', json={
                'username':'alice','password':'a-long-test-password'
            })
            self.assertEqual(cookie_login.status_code, 200, cookie_login.text)
            self.assertTrue(cookie_login.cookies.get('session'))
            self.assertEqual(cookie_client.get('/api/session').status_code, 200)

            self.assertTrue(disable_account('alice'))
            self.assertEqual(cookie_client.get('/api/session').status_code, 401)
            self.assertEqual(self.client.get('/api/session', headers={'Authorization':'Bearer '+token}).status_code, 401)
            self.assertEqual(self.client.post('/api/login', json={
                'username':'alice','password':'a-long-test-password'
            }).status_code, 403)
            self.assertEqual(cookie_client.post('/api/login', json={
                'username':'alice','password':'a-long-test-password'
            }).status_code, 403)
            cookie_client.close()

        with database.connect() as db:
            user_id = db.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memories WHERE user_id=?', (user_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversations WHERE user_id=?', (user_id,)).fetchone()[0], 1)
        self.assertTrue(enable_account('alice'))
        new_login = self.client.post('/api/login', json={
            'username':'alice','password':'a-long-test-password'
        })
        self.assertEqual(new_login.status_code, 200, new_login.text)
        reenabled = {'Authorization':'Bearer '+new_login.json()['token']}
        self.assertEqual(len(self.client.get('/api/memories', headers=reenabled).json()), 1)
        self.assertEqual(self.client.get('/api/conversations', headers=reenabled).json()[0]['id'], conversation['id'])

    def test_invitation_policy_is_reported_and_direct_api_cannot_bypass_it(self):
        with patch.dict(os.environ, {'DEMO_INVITATIONS_REQUIRED':'true'}):
            self.assertTrue(self.client.get('/api/session-mode').json()['invitation_required'])
            direct = self.client.post('/api/register', json={
                'username':'direct-bypass','password':'a-long-test-password'
            })
        self.assertEqual(direct.status_code, 400)

    def test_cookie_auth_is_http_only_and_protects_cookie_authenticated_writes(self):
        with patch.dict(os.environ, {'SESSION_COOKIE_AUTH':'true','SESSION_COOKIE_SECURE':'false'}):
            login_response=self.client.post('/api/login',json={
                'username':'alice','password':'a-long-test-password'
            })
            self.assertEqual(login_response.status_code,200)
            self.assertNotIn('token',login_response.json())
            cookie=login_response.cookies.get('session')
            self.assertTrue(cookie)
            set_cookie=login_response.headers['set-cookie'].lower()
            self.assertIn('httponly',set_cookie)
            self.assertIn('samesite=lax',set_cookie)
            self.assertEqual(self.client.get('/api/session').status_code,200)
            self.assertEqual(self.client.post('/api/conversations',json={}).status_code,403)
            self.assertEqual(self.client.post('/api/conversations',json={},headers={
                'origin':'https://attacker.example'
            }).status_code,403)
            self.assertEqual(self.client.post('/api/conversations',json={},headers={
                'origin':'https://attacker.example','authorization':'Basic invalid'
            }).status_code,403)
            allowed=self.client.post('/api/conversations',json={},headers={'origin':'http://testserver'})
            self.assertEqual(allowed.status_code,200,allowed.text)
            logged_out=self.client.post('/api/logout',headers={'origin':'http://testserver'})
            self.assertEqual(logged_out.status_code,200,logged_out.text)
            self.assertEqual(self.client.get('/api/session').status_code,401)

    def test_disabled_cookie_mode_rejects_previously_issued_cookie_but_accepts_bearer(self):
        with patch.dict(os.environ,{'SESSION_COOKIE_AUTH':'true','SESSION_COOKIE_SECURE':'false'}):
            cookie_login=self.client.post('/api/login',json={
                'username':'alice','password':'a-long-test-password'
            })
            self.assertEqual(cookie_login.status_code,200,cookie_login.text)
            old_cookie=cookie_login.cookies.get('session')
            self.assertTrue(old_cookie)
        self.client.cookies.clear()
        with patch.dict(os.environ,{'SESSION_COOKIE_AUTH':'false'}):
            old_cookie_response=self.client.get('/api/session',headers={'cookie':f'session={old_cookie}'})
            self.assertEqual(old_cookie_response.status_code,401)
            bearer_login=self.client.post('/api/login',json={
                'username':'alice','password':'a-long-test-password'
            })
            self.assertEqual(bearer_login.status_code,200,bearer_login.text)
            self.assertIn('token',bearer_login.json())
            bearer_response=self.client.get('/api/session',headers={
                'authorization':'Bearer '+bearer_login.json()['token']
            })
            self.assertEqual(bearer_response.status_code,200)

    def test_cookie_auth_sets_secure_attribute_when_configured(self):
        with patch.dict(os.environ,{'SESSION_COOKIE_AUTH':'true','SESSION_COOKIE_SECURE':'true'}):
            response=self.client.post('/api/login',json={
                'username':'alice','password':'a-long-test-password'
            })
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('secure',response.headers['set-cookie'].lower())

    def test_production_cookie_auth_refuses_insecure_cookie_configuration(self):
        with patch.dict(os.environ,{
            'APP_ENV':'production','SESSION_COOKIE_AUTH':'true','SESSION_COOKIE_SECURE':'false'
        }):
            with self.assertRaisesRegex(RuntimeError,'SESSION_COOKIE_SECURE must be enabled'):
                with TestClient(app):
                    pass

    def test_groq_daily_action_quota_is_user_and_service_limited(self):
        provider_response=Mock()
        provider_response.json.return_value={'choices':[{'message':{'content':'Mocked hosted reply.'}}]}
        request_limiter.clear()
        with patch.dict(os.environ,{
            'MODEL_PROVIDER':'groq','GROQ_API_KEY':'test-only-key',
            'MODEL_ACTIONS_PER_USER_PER_DAY':'1','MODEL_ACTIONS_PER_DAY':'1',
        },clear=False),patch('app.agent.requests.post',return_value=provider_response) as post:
            first=self.client.post('/api/chat',headers=self.alice,json={'message':'Explain a simple idea.'})
            second=self.client.post('/api/chat',headers=self.alice,json={'message':'Another simple idea.'})
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(second.status_code,429,second.text)
        self.assertIn('Retry-After',second.headers)
        self.assertEqual(post.call_count,1)
        self.assertEqual(len(self.client.get('/api/conversations',headers=self.alice).json()),1)


if __name__ == '__main__':
    unittest.main()
