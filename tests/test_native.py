import unittest
import tempfile
import shutil
import os
import time
import sys
import threading
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from xontrib_looseene.backend import IndexEngine, TextProcessor, DiskSegment


class TestTextProcessor(unittest.TestCase):
    """Тестирование обработки текста"""

    def test_process_basic(self):
        text = 'Git Commit'
        tokens = TextProcessor.process(text)
        self.assertEqual(tokens, ['git', 'commit'])

    def test_stemming(self):
        self.assertEqual(TextProcessor.process('running'), ['runn'])
        self.assertEqual(TextProcessor.process('dockers'), ['docker'])
        self.assertEqual(TextProcessor.process('list'), ['list'])

    def test_empty_text(self):
        """Проверка обработки пустого текста"""
        self.assertEqual(TextProcessor.process(''), [])
        self.assertEqual(TextProcessor.process(None), [])

    def test_special_characters(self):
        """Проверка фильтрации спецсимволов"""
        text = 'docker-compose --build'
        tokens = TextProcessor.process(text)
        # Только слова, без дефисов и дополнительных символов
        # 'docker' → 'dock' (стемминг убирает 'er')
        self.assertIn('dock', tokens)
        self.assertIn('compose', tokens)
        self.assertIn('build', tokens)


class TestIndexEngine(unittest.TestCase):
    """Тестирование основного движка"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.engine = IndexEngine('test_idx', self.test_dir)

    def tearDown(self):
        for seg in self.engine.segments:
            seg.close()
        shutil.rmtree(self.test_dir)

    def _create_doc(self, cmd, timestamp=None, cnt=None, cmt=''):
        if timestamp is None:
            timestamp = time.time_ns()
        doc = {'id': timestamp, 'inp': cmd, 'cmt': cmt}
        if cnt is not None:
            doc['cnt'] = cnt
        return doc

    def test_add_and_search_exact(self):
        doc = self._create_doc('docker run hello-world')
        self.engine.add(doc)
        results = self.engine.search('docker')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['inp'], 'docker run hello-world')

    def test_prefix_search(self):
        doc = self._create_doc('distribution update')
        self.engine.add(doc)
        results = self.engine.search('dis')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['inp'], 'distribution update')
        results_2 = self.engine.search('upda')
        self.assertEqual(len(results_2), 1)

    def test_deduplication_and_counts(self):
        """Проверка авто-инкремента счетчика"""
        cmd = 'git status'
        self.engine.add(self._create_doc(cmd))
        self.engine.add(self._create_doc(cmd))
        results = self.engine.search('git')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['cnt'], 2)

    def test_comments_update(self):
        cmd = 'ls -la'
        self.engine.add(self._create_doc(cmd))
        self.engine.add(self._create_doc(cmd, cmt='list files'))
        results = self.engine.search('ls')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['cmt'], 'list files')
        self.assertEqual(results[0]['cnt'], 2)

    def test_persistence_and_compaction(self):
        self.engine.add(self._create_doc('command one'))
        self.engine.flush()
        self.engine.add(self._create_doc('command two'))
        self.engine.flush()
        self.assertGreaterEqual(len(self.engine.segments), 2)
        self.engine.compact()

        for seg in self.engine.segments:
            seg.close()

        new_engine = IndexEngine('test_idx', self.test_dir)
        res_compact = new_engine.search('two')
        self.assertEqual(len(res_compact), 1)
        self.assertEqual(res_compact[0]['inp'], 'command two')
        self.assertEqual(new_engine.stats['total_docs'], 2)

        for seg in new_engine.segments:
            seg.close()

    def test_search_ranking(self):
        self.engine.add(self._create_doc('apple banana'))
        self.engine.add(self._create_doc('apple orange'))
        self.engine.add(self._create_doc('apple banana cherry'))
        results = self.engine.search('cherry')
        self.assertEqual(results[0]['inp'], 'apple banana cherry')

    # ============================================================
    # НОВЫЕ ТЕСТЫ ДЛЯ ВЫЯВЛЕНИЯ БАГОВ
    # ============================================================

    def test_stats_consistency_after_duplicates(self):
        """Тест бага #3: статистика не должна расти при дубликатах"""
        cmd = 'echo hello'

        # Первое добавление
        self.engine.add(self._create_doc(cmd))
        initial_docs = self.engine.stats['total_docs']
        initial_len = self.engine.stats['total_len']

        # Второе добавление (дубликат)
        self.engine.add(self._create_doc(cmd))

        # Статистика НЕ должна измениться
        self.assertEqual(self.engine.stats['total_docs'], initial_docs)
        self.assertEqual(self.engine.stats['total_len'], initial_len)

    def test_doc_freqs_recalculated_after_compact(self):
        """Тест бага #5: doc_freqs должны пересчитываться при компакции"""
        # Добавляем документы с одинаковым термином
        self.engine.add(self._create_doc('python script'))
        self.engine.add(self._create_doc('python code'))
        self.engine.flush()

        # Добавляем дубликат
        self.engine.add(self._create_doc('python script'))
        self.engine.flush()

        # До компакции doc_freqs['python'] может быть завышен
        before_compact = self.engine.stats['doc_freqs']['python']

        # Компактим
        self.engine.compact()

        # После компакции должно остаться только 2 документа (дубликат схлопнулся)
        self.assertEqual(self.engine.stats['total_docs'], 2)
        # doc_freqs['python'] должен быть 2 (не 3)
        self.assertEqual(self.engine.stats['doc_freqs']['python'], 2)

    def test_compact_with_closed_segments_no_crash(self):
        """Тест бага #1: compact не должен падать при чтении из закрытых сегментов"""
        # Создаем несколько сегментов
        for i in range(5):
            self.engine.add(self._create_doc(f'command {i}'))
            self.engine.flush()

        # Компактим - не должно быть исключений
        try:
            self.engine.compact()
            success = True
        except Exception as e:
            success = False
            print(f'Compact failed: {e}')

        self.assertTrue(success)

        # Проверяем что данные читаются
        results = self.engine.search('command')
        self.assertEqual(len(results), 5)

    def test_seen_meta_updated_on_duplicate(self):
        """Тест бага #2: seen_meta должен обновляться даже при раннем возврате"""
        cmd = 'git commit'

        # Первое добавление
        self.engine.add(self._create_doc(cmd, cmt='initial'))

        # Второе добавление с новым комментарием
        self.engine.add(self._create_doc(cmd, cmt='updated comment'))

        # Проверяем что seen_meta содержит обновленный комментарий
        import hashlib

        h = hashlib.md5(cmd.encode('utf-8')).hexdigest()
        self.assertEqual(self.engine.seen_meta[h]['cmt'], 'updated comment')
        self.assertEqual(self.engine.seen_meta[h]['cnt'], 2)

    def test_empty_query_returns_empty_results(self):
        """Проверка обработки пустого запроса"""
        self.engine.add(self._create_doc('some command'))
        results = self.engine.search('')
        self.assertEqual(len(results), 0)

    def test_search_with_no_matching_terms(self):
        """Проверка поиска по несуществующим терминам"""
        self.engine.add(self._create_doc('docker run'))
        results = self.engine.search('kubernetes')
        self.assertEqual(len(results), 0)

    def test_multiple_segments_search(self):
        """Проверка поиска по нескольким сегментам"""
        # Создаем 3 сегмента
        self.engine.add(self._create_doc('first segment'))
        self.engine.flush()

        self.engine.add(self._create_doc('second segment'))
        self.engine.flush()

        self.engine.add(self._create_doc('third segment'))
        self.engine.flush()

        # Поиск должен работать по всем сегментам
        results = self.engine.search('segment')
        self.assertEqual(len(results), 3)

    def test_compaction_preserves_all_unique_docs(self):
        """Проверка что компакция сохраняет все уникальные документы"""
        docs = ['docker build', 'docker run', 'git commit', 'git push', 'python script']

        for doc in docs:
            self.engine.add(self._create_doc(doc))
            self.engine.flush()

        # Добавляем дубликаты
        self.engine.add(self._create_doc('docker build'))
        self.engine.flush()

        self.engine.compact()

        # Должно остаться 5 уникальных документов
        self.assertEqual(self.engine.stats['total_docs'], 5)

        # Проверяем что все документы доступны
        for term in ['docker', 'git', 'python']:
            results = self.engine.search(term)
            self.assertGreater(len(results), 0)

    def test_total_len_consistency(self):
        """Проверка корректности total_len после компакции"""
        self.engine.add(self._create_doc('one two three'))
        self.engine.add(self._create_doc('four five'))
        self.engine.flush()

        # total_len должен быть 5 токенов
        expected_len = 5
        self.assertEqual(self.engine.stats['total_len'], expected_len)

        self.engine.compact()

        # После компакции total_len не должен измениться
        self.assertEqual(self.engine.stats['total_len'], expected_len)

    def test_concurrent_add_and_search(self):
        """Проверка thread-safety при одновременном добавлении и поиске"""
        results = []
        errors = []

        def add_docs():
            try:
                for i in range(50):
                    self.engine.add(self._create_doc(f'concurrent test {i}'))
            except Exception as e:
                errors.append(e)

        def search_docs():
            try:
                for _ in range(50):
                    res = self.engine.search('concurrent')
                    results.append(len(res))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_docs), threading.Thread(target=search_docs)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Не должно быть ошибок
        self.assertEqual(len(errors), 0)

    def test_segment_reloading_after_compact(self):
        """Проверка что сегменты корректно перезагружаются после компакции"""
        self.engine.add(self._create_doc('before compact'))
        self.engine.flush()

        old_segment_count = len(self.engine.segments)

        self.engine.compact()

        # После компакции должен быть 1 сегмент (merged)
        self.assertEqual(len(self.engine.segments), 1)

        # Добавляем новый документ
        self.engine.add(self._create_doc('after compact'))

        # Поиск должен находить оба документа
        results = self.engine.search('compact')
        self.assertEqual(len(results), 2)


class TestDiskSegment(unittest.TestCase):
    """Тесты для DiskSegment"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_segment_with_empty_files(self):
        """Тест бага #4: сегмент должен работать с пустыми файлами"""
        seg_path = Path(self.test_dir) / 'test_seg'
        seg_path.mkdir()

        # Создаем пустые файлы
        (seg_path / 'postings.bin').touch()
        (seg_path / 'docs.bin').touch()
        (seg_path / 'vocab.json').write_text('{}')
        (seg_path / 'doc_idx.json').write_text('{}')

        # Не должно быть исключений
        try:
            segment = DiskSegment(seg_path)
            segment.close()
            success = True
        except Exception as e:
            success = False
            print(f'Failed to load empty segment: {e}')

        self.assertTrue(success)

    def test_get_postings_nonexistent_term(self):
        """Проверка get_postings для несуществующего термина"""
        seg_path = Path(self.test_dir) / 'test_seg'
        seg_path.mkdir()
        (seg_path / 'postings.bin').touch()
        (seg_path / 'docs.bin').touch()
        (seg_path / 'vocab.json').write_text('{}')
        (seg_path / 'doc_idx.json').write_text('{}')

        segment = DiskSegment(seg_path)
        postings = list(segment.get_postings('nonexistent'))
        self.assertEqual(len(postings), 0)
        segment.close()

    def test_get_document_nonexistent_id(self):
        """Проверка get_document для несуществующего ID"""
        seg_path = Path(self.test_dir) / 'test_seg'
        seg_path.mkdir()
        (seg_path / 'postings.bin').touch()
        (seg_path / 'docs.bin').touch()
        (seg_path / 'vocab.json').write_text('{}')
        (seg_path / 'doc_idx.json').write_text('{}')

        segment = DiskSegment(seg_path)
        doc = segment.get_document(999999)
        self.assertIsNone(doc)
        segment.close()


class TestEdgeCases(unittest.TestCase):
    """Тесты граничных случаев"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.engine = IndexEngine('test_idx', self.test_dir)

    def tearDown(self):
        for seg in self.engine.segments:
            seg.close()
        shutil.rmtree(self.test_dir)

    def _create_doc(self, cmd, timestamp=None, cnt=None, cmt=''):
        if timestamp is None:
            timestamp = time.time_ns()
        doc = {'id': timestamp, 'inp': cmd, 'cmt': cmt}
        if cnt is not None:
            doc['cnt'] = cnt
        return doc

    def test_add_document_with_empty_command(self):
        """Проверка добавления документа с пустой командой"""
        self.engine.add(self._create_doc(''))
        self.engine.add(self._create_doc('   '))

        # Не должно добавиться ничего
        self.assertEqual(self.engine.stats['total_docs'], 0)

    def test_very_long_command(self):
        """Проверка обработки очень длинной команды"""
        long_cmd = ' '.join([f'word{i}' for i in range(1000)])
        self.engine.add(self._create_doc(long_cmd))

        results = self.engine.search('word500')
        self.assertEqual(len(results), 1)

    def test_special_unicode_characters(self):
        """Проверка обработки unicode символов"""
        self.engine.add(self._create_doc("echo 'привет мир'"))
        self.engine.add(self._create_doc("echo '你好世界'"))

        results = self.engine.search('echo')
        self.assertEqual(len(results), 2)

    def test_compact_single_segment(self):
        """Компакция с одним сегментом не должна ничего делать"""
        self.engine.add(self._create_doc('single doc'))
        self.engine.flush()

        self.assertEqual(len(self.engine.segments), 1)
        self.engine.compact()
        self.assertEqual(len(self.engine.segments), 1)


if __name__ == '__main__':
    unittest.main()
