import unittest

import backup_search_knowledge as knowledge


class BackupSearchKnowledgeTests(unittest.TestCase):
    def test_extract_terms_handles_chinese_and_ascii(self):
        terms = knowledge.extract_terms("合同审批通过，微信同号 pdf1914，承装修试资质")

        self.assertTrue(any("合同" in term for term in terms))
        self.assertTrue(any(term.startswith("承装") for term in terms))
        self.assertIn("pdf1914", terms)

    def test_chat_accumulator_builds_summary(self):
        acc = knowledge.ChatAccumulator(
            username="group@chatroom",
            chat="测试群",
            is_group=True,
        )
        acc.update(
            sender="张三",
            msg_type="text",
            timestamp=1760000000,
            text="合同审批通过",
        )
        acc.update(
            sender="李四",
            msg_type="image",
            timestamp=1760000300,
            text="[图片]",
        )

        summary = acc.finalize()

        self.assertEqual(summary["message_count"], 2)
        self.assertEqual(summary["participant_count"], 2)
        self.assertEqual(summary["top_sender"], "张三")
        self.assertTrue(summary["top_keywords"])


if __name__ == "__main__":
    unittest.main()
