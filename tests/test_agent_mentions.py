import unittest
from agent_catalog import CATALOG
from agent_mentions import select_agent

class AgentMentionTests(unittest.TestCase):
    def test_auto_without_mention(self):
        self.assertEqual(select_agent('Facturación mensual', 'auto', CATALOG, ['ventas']), (None, 'Facturación mensual'))

    def test_manual_selects_accessible_agent(self):
        self.assertEqual(select_agent('@ventas Muestra la facturación', 'manual', CATALOG, ['ventas']), ('ventas', 'Muestra la facturación'))

    def test_manual_requires_mention(self):
        with self.assertRaises(ValueError):
            select_agent('Muestra la facturación', 'manual', CATALOG, ['ventas'])

    def test_manual_denies_inaccessible_agent(self):
        with self.assertRaises(PermissionError):
            select_agent('@adhesiva OEE de RB4', 'manual', CATALOG, ['ventas'])

    def test_auto_mention_overrides(self):
        self.assertEqual(select_agent('@adhesiva OEE de RB4', 'auto', CATALOG, ['adhesiva']), ('adhesiva', 'OEE de RB4'))

    def test_email_is_not_mention(self):
        self.assertEqual(select_agent('Escribe a ibai@oribay.com', 'auto', CATALOG, ['ventas']), (None, 'Escribe a ibai@oribay.com'))

    def test_unknown_agent(self):
        with self.assertRaises(ValueError):
            select_agent('@expediciones Dime envíos', 'manual', CATALOG, ['ventas'])
