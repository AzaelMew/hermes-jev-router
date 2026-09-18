import unittest

from hermes_jev_router.plugin import _router_is_active


class PluginTests(unittest.TestCase):
    def test_virtual_model_aliases_activate_routing(self):
        self.assertTrue(_router_is_active("auto"))
        self.assertTrue(_router_is_active("jev-router"))
        self.assertFalse(_router_is_active("gpt-5.6-luna"))


# Created by Codex GPT-6 on 2026-09-17 16:48 PDT on ombee.
