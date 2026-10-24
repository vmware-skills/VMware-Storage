"""A wrong name must not send the model to check connectivity.

2026-10-24 conversation round: get_vsan_health with an unknown cluster said
"Run 'vmware-storage doctor' to verify connectivity." while get_iscsi_status
with an unknown host, whose message has the same shape, said the message
names the fix. Both are request errors.
"""

from vmware_storage.mcp_server.server import _DOCTOR_HINT, _FIX_IN_MESSAGE_HINT, _hint_for
from vmware_storage.ops.storage_paths import StoragePathError
from vmware_storage.ops.vsan import ClusterNotFoundError, VSANError


def test_unknown_cluster_says_the_message_has_the_fix():
    assert _hint_for(ClusterNotFoundError("Cluster 'x' not found. Available: cls01.")) == _FIX_IN_MESSAGE_HINT


def test_scope_errors_say_the_message_has_the_fix():
    assert _hint_for(StoragePathError("Host 'x' not found on this target.")) == _FIX_IN_MESSAGE_HINT


def test_other_vsan_failures_keep_doctor():
    # The SDK missing or an endpoint absent is about the environment, not the name.
    assert _hint_for(VSANError("vSAN 'x' endpoint was not found")) == _DOCTOR_HINT


def test_unknown_cluster_is_still_a_vsan_error():
    # Existing `except VSANError` callers keep catching it.
    assert issubclass(ClusterNotFoundError, VSANError)
