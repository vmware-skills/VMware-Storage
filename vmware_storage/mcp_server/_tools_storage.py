"""Storage MCP registrations sharing the server runtime.

The facade owns connection, error and Policy state; this module creates no second
server or connection manager. Returned aliases preserve the original Python API.
"""

from typing import Optional


def register_tools(mcp, runtime):
    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(
        risk_level="high",
        undo=runtime._acted_undo(
            "target_removed",
            "add_iscsi_target",
            "Inverse of remove_iscsi_target: re-add the iSCSI send target that was removed.",
        ),
    )
    def remove_iscsi_target(
        host_name: str,
        address: str,
        port: int = 3260,
        confirm: bool = False,
        target: Optional[str] = None,
    ) -> dict:
        """[WRITE] Remove an iSCSI send target from an ESXi host's software iSCSI adapter, then rescan all HBAs and VMFS volumes.

        Destructive: LUNs served only through this target become inaccessible
        after the rescan. Without confirm=True this only previews: it returns
        blast_radius (the static targets it discovered, the paths that go with
        them, the devices and VMFS datastores that lose all or some paths) and
        changes nothing. Show that to the user and get their explicit decision. Do
        not set confirm=True on your own because the user asked earlier — they
        have not seen what it cuts off yet. Refused when a datastore would lose
        every path (unmount it first), when the address:port is not configured or
        software iSCSI is disabled, and when any path or static target cannot be
        attributed. Reversible only by re-adding via add_iscsi_target.
        Returns a dict.

        Args:
            host_name: ESXi host name as shown in vCenter inventory.
            address: Configured iSCSI portal IP (IPv4/IPv6 literal; hostnames
                rejected). Must match the existing entry exactly.
            port: Configured iSCSI TCP port, 1-65535 (default 3260).
            confirm: False (default) returns the blast radius and changes nothing. True applies it.
            target: Optional vCenter/ESXi target name from config.
        """
        decision = runtime._gate.decide(confirm)
        try:
            si = runtime._get_connection(target)
            radius = runtime._gate.measure_remove_target(si, host_name, address, port)
            if not decision.act:
                return runtime._preview(radius)
            runtime._gate.refuse_if_blocked("remove_iscsi_target", host_name, radius)
            result = runtime._ops_remove_iscsi_target(si, host_name, address, port)
            runtime._safe_audit(
                target=target or "default",
                operation="iscsi_remove_target",
                resource=host_name,
                parameters={"host_name": host_name, "address": address, "port": port},
                result=result,
            )
            return {"action": "target_removed", "result": result, "blast_radius": radius}
        except Exception as e:
            runtime.logger.error("remove_iscsi_target failed: %s", e)
            return runtime._error_reply(e, "storage")

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="medium")
    def rescan_storage(
        host_name: str,
        confirm: bool = False,
        target: Optional[str] = None,
    ) -> dict:
        """[WRITE] Rescan all HBAs and VMFS volumes on an ESXi host to discover newly presented LUNs and datastores.

        Use this when a storage array presents new LUNs, or after out-of-band SAN
        changes. Not needed after add_iscsi_target /
        remove_iscsi_target — those rescan automatically. Without
        confirm=True this only previews: it returns blast_radius (the host, every
        adapter the rescan touches, the mounted VMFS volumes) and changes nothing.
        Show it to the user. Do not set confirm=True on your own because the user
        asked earlier — they have not seen the preview yet. Deletes nothing, but it
        is I/O-visible on the host and may take a minute or two with many paths.
        Refused when the host's storage view cannot be read. Returns a dict.

        Args:
            host_name: ESXi host name as shown in vCenter inventory (FQDN or IP).
                Errors if not found.
            confirm: False (default) returns the blast radius and changes nothing. True applies it.
            target: Optional vCenter/ESXi target name from config.
        """
        decision = runtime._gate.decide(confirm)
        try:
            si = runtime._get_connection(target)
            radius = runtime._gate.measure_rescan(si, host_name)
            if not decision.act:
                return runtime._preview(radius)
            runtime._gate.refuse_if_blocked("rescan_storage", host_name, radius)
            result = runtime._ops_rescan_storage(si, host_name)
            runtime._safe_audit(
                target=target or "default",
                operation="rescan_storage",
                resource=host_name,
                parameters={"host_name": host_name},
                result=result,
            )
            return {"action": "rescanned", "result": result, "blast_radius": radius}
        except Exception as e:
            runtime.logger.error("rescan_storage failed: %s", e)
            return runtime._error_reply(e, "storage")

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_vsan_health(
        cluster_name: str,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Get vSAN overall health, per-check-group health, and per-host disk-group layout for a cluster.

        Returns {cluster_name, vsan_enabled, overall_health,
        overall_health_description, health_queried, health_not_queried_reason,
        health_checked_at, test_groups: [{group_id, group_name, group_health}],
        host_count, hosts_read, hosts_not_read: [{host, reason}],
        disk_groups_complete,
        disk_groups: [{host, cache_disk, cache_size_gb, capacity_disks}]}.
        overall_health is what vSAN reports — green / yellow / red, and sometimes
        "unknown", which is vSAN's own answer. When the health service could not be
        asked at all, overall_health is null and health_queried is false with the
        reason: a null is "not measured", never a measurement. health_checked_at is
        the age of vCenter's cached summary (the same one Skyline Health shows);
        this tool reads the cache rather than triggering a full health run.
        Check disk_groups_complete before treating disk_groups as the cluster's
        inventory: vCenter cannot read a disconnected or notResponding host, and an
        empty disk_groups on such a cluster does NOT mean it has none — the unread
        hosts are named in hosts_not_read and in the message.
        If vSAN is not enabled, returns vsan_enabled=false with a message
        rather than an error. Use get_vsan_capacity for space usage instead. No side
        effects.

        Args:
            cluster_name: Cluster name exactly as shown in vCenter. Errors if the
                cluster is not found.
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime._ops_get_vsan_health(si, cluster_name)
        except Exception as e:
            runtime.logger.error("get_vsan_health failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_vsan_capacity(
        cluster_name: str,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Get space usage of a cluster's vSAN datastore for capacity planning.

        Returns {cluster_name, vsan_enabled, datastore_name, accessible, total_gb,
        used_gb, free_gb, usage_pct}. When the vSAN datastore is inaccessible
        (accessible=false) the four figures are null with a message, not 0 — vCenter
        answers 0 for a datastore it cannot reach, and 0 GB used reads as a healthy
        empty datastore. accessible=null means the summary did not say. If vSAN is
        not enabled on the cluster, returns vsan_enabled=false with an explanatory
        message rather than an error; errors only if the cluster name is not found.
        Use get_vsan_health for disk-group layout and host details; use list_all_datastores for non-vSAN
        (VMFS/NFS) datastore usage. No side effects.

        Args:
            cluster_name: Cluster name exactly as shown in vCenter. Errors if the
                cluster is not found.
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime._ops_get_vsan_capacity(si, cluster_name)
        except Exception as e:
            runtime.logger.error("get_vsan_capacity failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_vsan_efficiency(
        cluster_name: str,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Get vSAN data-efficiency (deduplication + compression) status for a cluster.

        Returns {cluster_name, vsan_enabled, dedup_enabled, compression_enabled}.
        Reads it via the vSAN Management SDK (VsanVcClusterConfigSystem), not base
        pyVmomi. When vSAN reports no data-efficiency config (space efficiency off,
        or an OSA cluster without it), dedup_enabled/compression_enabled come back
        null with a message rather than a fabricated false. Errors only if the
        cluster name is not found. Use get_vsan_capacity for space usage and
        get_vsan_health for disk-group layout. No side effects.

        Note: vSAN Global Deduplication and vSAN-to-vSAN replication are NOT
        exposed here — neither has a verified SDK object (global dedup has no
        distinct field; v2v replication lives in the separate vSAN Data Protection
        plane). Use the vCenter/vSAN UI for those.

        Args:
            cluster_name: Cluster name exactly as shown in vCenter (case-sensitive).
                Errors if the cluster is not found — vmware-storage has no
                cluster-listing tool; get names from vmware-monitor list_all_clusters.
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime._ops_get_vsan_efficiency(si, cluster_name)
        except Exception as e:
            runtime.logger.error("get_vsan_efficiency failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_fc_adapters(
        cluster: Optional[str] = None,
        host: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] List Fibre Channel HBAs (FC and FCoE) per ESXi host: vmhba, model, driver, status, port type, WWPN/WWNN and reported link speed.

        Use this for "which FC adapters does each host have" or to find a
        host's WWPNs; use list_storage_device_paths for devices and paths behind them.
        Scope with cluster OR host; with neither, every host on the target is
        read (only the adapter list is fetched, so this stays cheap).

        Returns the list envelope ('items', 'returned', 'total', 'truncated',
        'next_offset') plus hosts_without_fc (read, no FC HBA) and
        hosts_not_read [{host, reason}]. A host in hosts_not_read was NOT read —
        never report it as having no FC adapters. speed_reported is the raw
        vSphere value: the API documents bits per second, but hosts commonly report
        Gbit/s, so it is not converted. Reads host config only; no rescans.

        Args:
            cluster: Cluster name exactly as in vCenter. Omit to use host or the whole target.
            host: ESXi host name exactly as in vCenter inventory (FQDN or IP).
            limit: Rows per page, 1-200 (default 50).
            offset: Rows to skip; pass the previous page's next_offset.
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime._ops_list_fc_adapters(
                si, cluster=cluster, host=host, limit=limit, offset=offset
            )
        except Exception as e:
            runtime.logger.error("list_fc_adapters failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_storage_device_paths(
        cluster: Optional[str] = None,
        host: Optional[str] = None,
        datastore: Optional[str] = None,
        device: Optional[str] = None,
        adapter: Optional[str] = None,
        only_differences: bool = False,
        limit: int = 50,
        offset: int = 0,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] SCSI multipath state per device (NAA) across the hosts of one scope: which hosts see it, path counts and states, working paths, adapters, target WWPN, PSP/SATP policy, and the VMFS datastores on it.

        Use for: "does datastore X have dead or disabled paths on any host"
        (datastore=X), "which hosts see naa.… and through which adapters"
        (cluster + device), "do hosts see different numbers of paths"
        (cluster + only_differences=true), "which devices and datastores depend on
        vmhba2" (host + adapter). Exactly one of cluster, host or datastore is
        required — this tool will not read every host at once.

        Per device: shared (reached over FC/iSCSI or seen by 2+ hosts), seen_by,
        not_seen_on (hosts that WERE read and do not see a shared device; a disk
        inside one host is never listed), path_count_differs,
        states_needing_attention
        (dead/disabled only) and per-host {paths_total, by_state, working_paths,
        policy, satp, adapters}; with adapter set, also paths_via_adapter and
        only_paths_via_adapter (matched by vmhba name, which can be a different
        card on each host of a cluster). Per-path detail is included when device or
        datastore is given. Devices needing attention sort first; 'summary'
        counts the whole result, not just this page.

        Gotchas: hosts in hosts_not_read were NOT read (refused, not connected —
        vCenter's copy of a lost host's config may be stale — or missing from the
        reply) and are never in
        not_seen_on — when complete is false, say which hosts are unknown instead
        of concluding a device is missing. States are as vSphere reports them:
        'standby' can be normal (active/passive arrays), and a path count does not
        prove independent fabrics. NFS/vSAN/vVol datastores have no SCSI paths
        (scope_note says so). NVMe-oF namespaces may not appear. No rescans.

        Args:
            cluster: Cluster name exactly as in vCenter.
            host: ESXi host name exactly as in vCenter inventory.
            datastore: Datastore name; scopes to the hosts that mount it and its VMFS extents.
            device: Canonical name (e.g. naa.60060e80...) or display name; case-insensitive.
            adapter: vmhba name (e.g. vmhba2) to keep devices with a path through it.
            only_differences: Keep only devices whose visibility or path count
                differs across the hosts that were read.
            limit: Devices per page, 1-200 (default 50).
            offset: Devices to skip; pass the previous page's next_offset.
            target: Optional vCenter/ESXi target name from config.
        """
        try:
            si = runtime._get_connection(target)
            return runtime.device_paths(
                si,
                cluster=cluster,
                host=host,
                datastore=datastore,
                device=device,
                adapter=adapter,
                only_differences=only_differences,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            runtime.logger.error("list_storage_device_paths failed: %s", e)
            return {"error": runtime._safe_error(e, "storage"), "hint": runtime._hint_for(e)}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_access_info(
        tool: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Which account this server uses, and whether it holds each tool's vSphere privileges.

        Use this when a tool fails with NoPermission, or before a write, to check
        what the configured account may do. Asks vCenter/ESXi about the current
        session (needs only System.View, so any account can ask about itself).
        Returns target, account, the privilege states, and for one tool its
        requires/verdict/meaning; without tool, every tool grouped by verdict.
        Gotchas: privileges are checked on the root folder only. A verdict of
        not_granted_at_root is NOT "no access" — the privilege may be granted on
        a specific cluster, host or datastore, where the tool then works.
        undetermined means vCenter did not answer; never read it as granted.

        Args:
            tool: One tool name on this server, e.g. "add_iscsi_target". Omit to summarize every tool.
            target: Optional vCenter/ESXi target name from config.
        """
        unknown = runtime.unknown_tool_message(
            tool, runtime.REQUIREMENTS
        )  # no login for a misspelled name
        if unknown:
            return {"error": unknown}
        try:
            mgr = runtime._get_conn_mgr()
            name = mgr.resolve_target_name(target)
            si = mgr.connect(name)
            return runtime.vsphere_access_info(
                si, target=name, requirements=runtime.REQUIREMENTS, tool=tool
            )
        except Exception as e:
            runtime.logger.error("get_access_info failed: %s", e)
            return {
                "error": runtime._safe_error(e, "storage"),
                "hint": runtime._hint_for(e),
            }

    return (
        remove_iscsi_target,
        rescan_storage,
        get_vsan_health,
        get_vsan_capacity,
        get_vsan_efficiency,
        list_fc_adapters,
        list_storage_device_paths,
        get_access_info,
    )
