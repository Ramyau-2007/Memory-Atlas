import unittest
import sqlite3
import tempfile
from pathlib import Path
from app.db import init_db, connect
from app.auth import register, login
from app.memory import (
    add_memory, classify, decision_from_analysis, list_memories, needs_memory_analysis,
    retrieve, update_memory, soft_delete, recover_deleted, permanently_delete,
    restore_version, validate_memory_analysis, versions,
    deactivate_unedited_source_memories,
)

class MemoryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory(); self.db=self.temp.name+'/test.sqlite3';init_db(self.db)
        with connect(self.db) as conn:
            conn.execute("INSERT INTO users(username,password_hash) VALUES('alice','not-used')")
            conn.execute("INSERT INTO users(username,password_hash) VALUES('bob','not-used')")
    def tearDown(self): self.temp.cleanup()
    def test_classification_privacy_and_temporary_rules(self):
        self.assertTrue(classify('I prefer concise explanations')['retain'])
        self.assertTrue(classify('I am vegetarian')['retain'])
        self.assertTrue(classify('my fav prog lang is python')['retain'])
        self.assertTrue(classify('i lyk python for coding')['retain'])
        self.assertFalse(classify('my API key is abc123')['retain'])
        self.assertTrue(classify('What is the weather?')['temporary'])

    def test_candidate_gate_covers_informal_and_paraphrased_personal_messages(self):
        examples = (
            'my fav prog lang is python', 'i lyk python for coding',
            'python is the lang i like most', 'u remember which lang i like?',
        )
        for message in examples:
            with self.subTest(message=message):
                self.assertTrue(needs_memory_analysis(message))
        self.assertFalse(needs_memory_analysis('Explain how a Python loop works.'))

    def test_fallback_classifier_preserves_negation_and_rejects_non_assertions(self):
        self.assertTrue(classify("I don't like Python")["retain"])
        for message in (
            'What language do I like?', 'If I liked Python, I would use it.',
            'Maybe I like Python, lol', 'I like Python, just kidding',
        ):
            with self.subTest(message=message):
                self.assertFalse(classify(message)['retain'])

    def test_analysis_validation_and_decisions_are_conservative(self):
        def result(message_type='assertion', about_user=True, confidence=0.96,
                   content='The user prefers Python as a programming language.',
                   category='preference', polarity='positive', terms=None):
            return {
                'message_type': message_type, 'about_user': about_user, 'confidence': confidence,
                'candidate': None if message_type != 'assertion' else {
                    'content': content, 'category': category, 'polarity': polarity,
                },
                'search_terms': terms or ['programming language', 'favorite', 'Python'],
            }
        positive = validate_memory_analysis(result())
        self.assertTrue(decision_from_analysis('my fav prog lang is python', positive)['retain'])
        negative = validate_memory_analysis(result(content='The user dislikes Python.', polarity='negative'))
        decision = decision_from_analysis("I don't like Python", negative)
        self.assertTrue(decision['retain'])
        self.assertIn('negative polarity', decision['reason'])
        self.assertIn('dislikes Python', decision['content'])
        self.assertFalse(decision_from_analysis("I don't like Python", positive)['retain'])

        for message_type in ('question', 'hypothetical', 'joke', 'uncertain'):
            analysis = validate_memory_analysis(result(message_type=message_type))
            self.assertIsNotNone(analysis)
            self.assertFalse(decision_from_analysis('example text', analysis)['retain'])
        self.assertFalse(decision_from_analysis('example text', validate_memory_analysis(
            result(confidence=0.84)))['retain'])
        self.assertFalse(decision_from_analysis('example text', validate_memory_analysis(
            result(about_user=False)))['retain'])

        malformed = [
            [], {'message_type': [], 'about_user': True, 'confidence': 1, 'candidate': None, 'search_terms': []},
            result(category=[]), result(confidence=True), result(terms=['x' * 41]),
            result(content='My secret API key is value'),
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertIsNone(validate_memory_analysis(payload))
    def test_memory_lifecycle_and_version_restore(self):
        mid=add_memory(1,'I prefer Python','preference',3,'Preference',db_path=self.db)
        self.assertTrue(update_memory(1,mid,'I prefer Python examples','preference',4,db_path=self.db))
        self.assertEqual(len(versions(1,mid,self.db)),2)
        self.assertTrue(restore_version(1,mid,1,self.db))
        self.assertEqual(len(versions(1,mid,self.db)),3)
        self.assertEqual(list_memories(1,db_path=self.db)[0]['content'],'I prefer Python')
        self.assertTrue(soft_delete(1,mid,self.db));self.assertEqual(list_memories(1,db_path=self.db),[])
    def test_retrieval_is_relevant_and_user_scoped(self):
        add_memory(1,'I prefer Python examples','preference',3,'Preference',db_path=self.db)
        add_memory(2,'I prefer hiking trails','preference',3,'Preference',db_path=self.db)
        found=retrieve(1,'Can you show Python examples?',db_path=self.db)
        self.assertEqual([x['content'] for x in found],['I prefer Python examples'])

    def test_source_memory_linking_exclusion_and_version_safe_deactivation(self):
        with connect(self.db) as db:
            db.execute("INSERT INTO conversations(user_id,title) VALUES(1,'source')")
            cid=db.execute("SELECT id FROM conversations WHERE user_id=1").fetchone()[0]
            message_id=db.execute("INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)",
                                  (cid,'user','I prefer Python')).lastrowid
        mid=add_memory(1,'I prefer Python examples','preference',3,'Automatic',db_path=self.db,
                       source_message_id=message_id)
        self.assertEqual(retrieve(1,'Python',db_path=self.db,exclude_ids=[mid]),[])
        with connect(self.db) as db:
            deactivated,preserved=deactivate_unedited_source_memories(db,1,[message_id])
        self.assertEqual(deactivated,[mid]); self.assertEqual(preserved,[])
        self.assertEqual(list_memories(1,db_path=self.db),[])
        self.assertEqual(len(versions(1,mid,self.db)),1)

    def test_fts_hybrid_retrieval_ranks_relevant_and_excludes_deleted_temporary_and_other_users(self):
        favorite=add_memory(1,'The user prefers Python for programming examples','preference',3,
                            'explicit preference',db_path=self.db)
        distractor=add_memory(1,'The user enjoys snakes and nature documentaries','fact',5,
                              'unrelated',db_path=self.db)
        deleted=add_memory(1,'The user likes Java programming','preference',5,'old preference',db_path=self.db)
        temporary=add_memory(1,'The user prefers Python jokes','preference',5,'temporary',temporary=True,db_path=self.db)
        other=add_memory(2,'The user prefers Python for programming','preference',5,'other account',db_path=self.db)
        self.assertTrue(soft_delete(1,deleted,self.db))
        matches=retrieve(1,'Which coding language does the user prefer?',db_path=self.db,
                         search_terms=['programming language Python preference'])
        ids=[row['id'] for row in matches]
        self.assertEqual(ids[0],favorite)
        self.assertNotIn(deleted,ids)
        self.assertNotIn(temporary,ids)
        self.assertNotIn(other,ids)
        self.assertNotIn(distractor,ids)
        self.assertIn('FTS5',matches[0]['retrieval_reason'])
        with connect(self.db) as db:
            fts_ids={row[0] for row in db.execute('SELECT rowid FROM memory_search_fts')}
        self.assertIn(favorite,fts_ids); self.assertNotIn(deleted,fts_ids); self.assertNotIn(temporary,fts_ids)
        self.assertTrue(recover_deleted(1,deleted,self.db))
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_search_fts WHERE rowid=?',(deleted,)).fetchone()[0],1)
        self.assertTrue(soft_delete(1,deleted,self.db))
        self.assertTrue(permanently_delete(1,deleted,self.db))
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_search_fts WHERE rowid=?',(deleted,)).fetchone()[0],0)

    def test_fts_migration_backfills_existing_records_without_changing_versions(self):
        mid=add_memory(1,'Existing Python preference','preference',3,'created before index',db_path=self.db)
        with connect(self.db) as db:
            version_count=db.execute('SELECT COUNT(*) FROM memory_versions WHERE memory_id=?',(mid,)).fetchone()[0]
            db.execute('DROP TRIGGER memories_fts_insert')
            db.execute('DROP TRIGGER memories_fts_update')
            db.execute('DROP TRIGGER memories_fts_delete')
            db.execute('DROP TABLE memory_search_fts')
        init_db(self.db)
        self.assertEqual([item['id'] for item in retrieve(1,'Python preference',db_path=self.db)],[mid])
        self.assertEqual(len(versions(1,mid,self.db)),version_count)

    def test_schema_upgrade_adds_nullable_message_link_without_touching_records(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path=str(Path(directory)/'pre-link.sqlite3')
            init_db(legacy_path)
            with connect(legacy_path) as db:
                db.execute("INSERT INTO users(username,password_hash) VALUES('legacy','hash')")
            old_memory=add_memory(1,'Legacy durable memory','fact',2,'pre-upgrade',db_path=legacy_path)
            # Simulate the previous schema by building legacy tables separately.
            legacy=sqlite3.connect(legacy_path)
            try:
                legacy.execute('PRAGMA foreign_keys=OFF')
                legacy.execute('ALTER TABLE memories RENAME TO memories_new')
                legacy.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, content TEXT NOT NULL, category TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2, source TEXT NOT NULL, reason TEXT NOT NULL, temporary INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
                legacy.execute('INSERT INTO memories(id,user_id,content,category,priority,source,reason,temporary,deleted_at,created_at,updated_at) SELECT id,user_id,content,category,priority,source,reason,temporary,deleted_at,created_at,updated_at FROM memories_new')
                legacy.execute('DROP TABLE memories_new')
                legacy.commit()
            finally: legacy.close()
            init_db(legacy_path)
            with connect(legacy_path) as db:
                row=db.execute('SELECT id,content,source_message_id FROM memories WHERE id=?',(old_memory,)).fetchone()
                self.assertEqual(tuple(row),(old_memory,'Legacy durable memory',None))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_versions WHERE memory_id=?',(old_memory,)).fetchone()[0],1)
                self.assertIn('source_message_id',{item[1] for item in db.execute('PRAGMA table_info(memories)')})

    def test_normalized_search_terms_retrieve_paraphrase_without_cross_user_leak(self):
        add_memory(1, "The user's favorite programming language is Python.", 'preference', 3, 'Normalized assertion', db_path=self.db)
        add_memory(2, "The user's favorite programming language is Java.", 'preference', 3, 'Other account', db_path=self.db)
        query='u remember which lang i like?'
        self.assertEqual(retrieve(1, query, db_path=self.db), [])
        found=retrieve(1, query, db_path=self.db, search_terms=['favorite programming language Python'])
        self.assertEqual([item['content'] for item in found], ["The user's favorite programming language is Python."])

    def test_database_initialization_adds_event_table_without_replacing_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = str(Path(directory) / 'legacy.sqlite3')
            legacy = sqlite3.connect(legacy_path)
            try:
                legacy.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                legacy.execute("INSERT INTO users(username,password_hash) VALUES('existing-user','existing-hash')")
                legacy.commit()
            finally:
                legacy.close()
            init_db(legacy_path)
            with connect(legacy_path) as migrated:
                user = migrated.execute('SELECT username,password_hash FROM users WHERE id=1').fetchone()
                event_table = migrated.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_events'").fetchone()
                self.assertEqual(tuple(user), ('existing-user', 'existing-hash'))
                self.assertIsNotNone(event_table)

if __name__=='__main__': unittest.main()
