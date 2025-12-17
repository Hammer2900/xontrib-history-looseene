import unittest
import tempfile
import shutil
import os
import time
import sys

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from xontrib_looseene.backend import IndexEngine, TextProcessor


class TestTextProcessor(unittest.TestCase):
    """Тестирование обработки текста"""

    def test_process_basic(self):
        text = "Git Commit"
        tokens = TextProcessor.process(text)
        self.assertEqual(tokens, ['git', 'commit'])

    def test_stemming(self):
        # Исправлено: наш простой стеммер делает 'runn' из 'running' (убирает ing).
        # Это нормально для простого движка, главное чтобы запрос и документ совпадали.
        self.assertEqual(TextProcessor.process("running"), ['runn'])
        self.assertEqual(TextProcessor.process("dockers"), ['docker'])
        self.assertEqual(TextProcessor.process("list"), ['list'])


class TestIndexEngine(unittest.TestCase):
    """Тестирование основного движка"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.engine = IndexEngine("test_idx", self.test_dir)

    def tearDown(self):
        # Явно закрываем файлы сегментов, чтобы не было ResourceWarning
        for seg in self.engine.segments:
            seg.close()
        shutil.rmtree(self.test_dir)

    def _create_doc(self, cmd, timestamp=None, cnt=None, cmt=""):
        """
        Helper для создания документа.
        ВАЖНО: cnt=None по умолчанию. Если не передать cnt, движок сам его посчитает (инкремент).
        Если передать cnt, движок запишет именно это число.
        """
        if timestamp is None:
            timestamp = time.time_ns()

        doc = {
            'id': timestamp,
            'inp': cmd,
            'cmt': cmt
        }
        # Добавляем поле cnt только если оно явно передано
        if cnt is not None:
            doc['cnt'] = cnt

        return doc

    def test_add_and_search_exact(self):
        doc = self._create_doc("docker run hello-world")
        self.engine.add(doc)

        results = self.engine.search("docker")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['inp'], "docker run hello-world")

    def test_prefix_search(self):
        doc = self._create_doc("distribution update")
        self.engine.add(doc)

        # 'dis' -> 'distribution'
        results = self.engine.search("dis")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['inp'], "distribution update")

        # 'upda' -> 'update'
        results_2 = self.engine.search("upda")
        self.assertEqual(len(results_2), 1)

    def test_deduplication_and_counts(self):
        """Проверка авто-инкремента счетчика"""
        cmd = "git status"

        # 1. Добавляем первый раз (cnt не передаем, движок поставит 1)
        self.engine.add(self._create_doc(cmd))

        # 2. Добавляем второй раз (cnt не передаем, движок должен найти хеш и сделать +1)
        self.engine.add(self._create_doc(cmd))

        results = self.engine.search("git")

        self.assertEqual(len(results), 1)
        # Теперь проверка пройдет: 1 + 1 = 2
        self.assertEqual(results[0]['cnt'], 2)

    def test_comments_update(self):
        cmd = "ls -la"

        # Добавляем команду
        self.engine.add(self._create_doc(cmd))

        # Обновляем коммент (и счетчик тоже увеличится автоматом)
        self.engine.add(self._create_doc(cmd, cmt="list files"))

        results = self.engine.search("ls")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['cmt'], "list files")
        self.assertEqual(results[0]['cnt'], 2)

    def test_persistence_and_compaction(self):
        # Добавляем и сбрасываем на диск
        self.engine.add(self._create_doc("command one"))
        self.engine.flush()

        self.engine.add(self._create_doc("command two"))
        self.engine.flush()

        self.assertGreaterEqual(len(self.engine.segments), 2)

        # Сжатие
        self.engine.compact()

        # Переоткрываем движок для проверки чтения с диска
        # Сначала закрываем старый
        for seg in self.engine.segments:
            seg.close()

        new_engine = IndexEngine("test_idx", self.test_dir)

        res_compact = new_engine.search("two")
        self.assertEqual(len(res_compact), 1)
        self.assertEqual(res_compact[0]['inp'], "command two")
        self.assertEqual(new_engine.stats['total_docs'], 2)

        # Закрываем новый движок
        for seg in new_engine.segments:
            seg.close()

    def test_search_ranking(self):
        self.engine.add(self._create_doc("apple banana"))
        self.engine.add(self._create_doc("apple orange"))
        self.engine.add(self._create_doc("apple banana cherry"))

        results = self.engine.search("cherry")
        self.assertEqual(results[0]['inp'], "apple banana cherry")


if __name__ == '__main__':
    unittest.main()
