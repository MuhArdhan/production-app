"""Isolated regression checks for combining FU114 and QZ/serial labels."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from production_app.api import qz_signing, work_order_labels


class TestLabelStageCompatibility(TestCase):
	def setUp(self):
		for module in (qz_signing, work_order_labels):
			for name, replacement in (
				("_", lambda text: text),
				("skips_prepacking", Mock(return_value=False)),
			):
				patcher = patch.object(module, name, replacement)
				patcher.start()
				self.addCleanup(patcher.stop)
		throw = patch.object(work_order_labels.frappe, "throw", side_effect=ValueError)
		throw.start()
		self.addCleanup(throw.stop)
		enrich = patch.object(work_order_labels, "_enrich_units", side_effect=self.enrich)
		enrich.start()
		self.addCleanup(enrich.stop)

	@staticmethod
	def enrich(rows):
		rows[0].display_conversion_factor = 5

	def work_order(self, skip, confirmed=True, good=12):
		stage = "postpacking" if skip else "prepacking"
		values = {f"custom_{stage}_confirmed": confirmed, f"custom_good_qty_{stage}": good}
		wo = Mock(production_item="ITEM-TEST")
		wo.get.side_effect = values.get
		return wo

	def test_count_uses_correct_confirmed_stage(self):
		for skip in (False, True):
			with self.subTest(skip_prepacking=skip):
				work_order_labels.skips_prepacking.return_value = skip
				self.assertEqual(work_order_labels._label_count(self.work_order(skip), 2), 5)

	def test_other_stage_confirmation_cannot_bypass_guard(self):
		for skip in (False, True):
			with self.subTest(skip_prepacking=skip):
				work_order_labels.skips_prepacking.return_value = skip
				with self.assertRaises(ValueError):
					work_order_labels._label_count(self.work_order(not skip), 0)

	def test_zero_and_nonfinite_good_rejected_for_both_stages(self):
		for skip in (False, True):
			work_order_labels.skips_prepacking.return_value = skip
			for good in (0, -1, float("nan"), float("inf")):
				with self.subTest(skip_prepacking=skip, good=good), self.assertRaises(ValueError):
					work_order_labels._label_count(self.work_order(skip, good=good), 0)

	def test_invalid_extra_and_maximum_guard_preserved(self):
		for extra in (-1, "1.5", "abc", 10000):
			with self.subTest(extra=extra), self.assertRaises(ValueError):
				work_order_labels._label_count(self.work_order(False), extra)

	def test_signing_accepts_correct_stage_and_checks_read_permission(self):
		with patch.object(qz_signing.frappe, "session", SimpleNamespace(user="test@example.com")):
			for skip in (False, True):
				qz_signing.skips_prepacking.return_value = skip
				wo = self.work_order(skip)
				with self.subTest(skip_prepacking=skip), patch.object(qz_signing.frappe, "get_doc", return_value=wo):
					self.assertIs(qz_signing._check_work_order("WO-TEST"), wo)
					wo.check_permission.assert_called_once_with("read")

	def test_signing_rejects_confirmation_of_wrong_stage(self):
		with patch.object(qz_signing.frappe, "session", SimpleNamespace(user="test@example.com")):
			for skip in (False, True):
				qz_signing.skips_prepacking.return_value = skip
				with self.subTest(skip_prepacking=skip), patch.object(qz_signing.frappe, "get_doc", return_value=self.work_order(not skip)):
					with self.assertRaises(ValueError):
						qz_signing._check_work_order("WO-TEST")
