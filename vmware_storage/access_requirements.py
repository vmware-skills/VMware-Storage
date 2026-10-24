"""vSphere privileges each MCP tool needs (HLD §17 D-17), for ``get_access_info``.

Derived from the pyVmomi calls each tool's ops make, using pyVmomi's own type
metadata (``_GetMethodInfo(name).privId`` / property ``privId``):

* finding hosts, clusters and datastores goes through a ContainerView
  (``ViewManager.CreateContainerView`` -> System.View) and the PropertyCollector
  (``ContinueRetrievePropertiesEx`` -> System.View); reading their ``config``,
  ``summary`` and ``storageDeviceInfo`` properties is System.Read;
* every ``HostStorageSystem`` change this skill makes —
  ``UpdateSoftwareInternetScsiEnabled``, ``Add/RemoveInternetScsiSendTargets``,
  ``RescanAllHba``, ``RescanVmfs`` — is Host.Config.Storage;
* the vSAN SDK calls (``QueryClusterHealthSummary``, ``VsanClusterGetConfig``)
  carry no privilege in the metadata beyond the reads around them.

The family gate (scripts/lib/access_requirements_cover_tools.py) checks that the
keys are exactly the registered tools and that every id is one vSphere defines.
"""

from __future__ import annotations

_READ = ["System.View", "System.Read"]
_HOST_STORAGE_CHANGE = ["System.View", "System.Read", "Host.Config.Storage"]

REQUIREMENTS: dict[str, list[str]] = {
    "list_all_datastores": _READ,
    "get_datastore_vm_usage": _READ,
    # HostDatastoreBrowser.SearchDatastoreSubFolders_Task has no privId in
    # pyVmomi's metadata; vSphere checks Datastore.Browse on the datastore
    # itself (a reviewed extra in the family gate; verify on a live vCenter).
    "browse_datastore": [*_READ, "Datastore.Browse"],
    "scan_datastore_images": [*_READ, "Datastore.Browse"],
    "list_cached_images": [],  # reads ~/.vmware-storage/image_registry.json only
    "enable_iscsi": _HOST_STORAGE_CHANGE,
    "get_iscsi_status": _READ,
    "add_iscsi_target": _HOST_STORAGE_CHANGE,
    "remove_iscsi_target": _HOST_STORAGE_CHANGE,
    "rescan_storage": _HOST_STORAGE_CHANGE,
    "get_vsan_health": _READ,
    "get_vsan_capacity": _READ,
    "get_vsan_efficiency": _READ,
    "list_fc_adapters": _READ,
    "list_storage_device_paths": _READ,
    # AuthorizationManager.HasPrivilegeOnEntities / RetrieveAllPermissions /
    # roleList are all System.View.
    "get_access_info": ["System.View"],
}
