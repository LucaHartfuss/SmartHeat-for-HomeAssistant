from unittest.mock import Mock, patch

import pytest

from heizungsbruecke.supervisor_api import set_own_options


def test_set_own_options_posts_correct_payload():
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.supervisor_api.requests.post", return_value=mock_response) as mock_post:
        set_own_options("http://supervisor", "test-token", {"tenant_id": "wohnung1"})

    mock_post.assert_called_once_with(
        "http://supervisor/addons/self/options",
        headers={"Authorization": "Bearer test-token"},
        json={"options": {"tenant_id": "wohnung1"}},
        timeout=10,
    )


def test_set_own_options_raises_on_http_error():
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.supervisor_api.requests.post", return_value=mock_response):
        with pytest.raises(RuntimeError):
            set_own_options("http://supervisor", "test-token", {})
