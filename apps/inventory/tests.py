from importlib import import_module

from django.test import SimpleTestCase


class InventoryViewsImportTests(SimpleTestCase):
    def test_views_module_imports_without_pandas_dependency(self):
        module = import_module('apps.inventory.views')

        self.assertIsNotNone(module.EquipmentItemViewSet)
