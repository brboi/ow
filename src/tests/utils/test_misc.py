from ow.utils.display import counts


class TestDisplayHelpers:

    def test_counts_nothing(self):
        result = counts(0, 0)
        assert "0" in result or result in [""]

    def test_counts_behind_only(self):
        result = counts(3, 0)
        assert "3" in result

    def test_counts_ahead_only(self):
        result = counts(0, 5)
        assert "5" in result

    def test_counts_both(self):
        result = counts(2, 3)
        assert "2" in result
        assert "3" in result
