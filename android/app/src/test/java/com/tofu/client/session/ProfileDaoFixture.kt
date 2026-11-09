package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.ProfileDao

/**
 * Scoped-save ports for DAO fixtures. Their update/cookie/recency fault hooks
 * remain live; production uses atomic Room SQL, executed separately by
 * tests/mobile/test_android_profile_save_sql.py. Race fixtures override these
 * ports to inject immediately before/after the atomic query decision.
 */
interface ProfileDaoFixture : ProfileDao {
    override suspend fun saveConnectionIfCurrent(
        id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType,
        expectedProjectPath: String?, alias: String, baseUrl: String, authType: AuthType,
        instanceUuid: String?, projectPath: String?,
    ): Int {
        val current = getById(id) ?: return 0
        if (current.kind != KernelKind.REMOTE_URL || current.baseUrl != expectedUrl || current.alias != expectedAlias ||
            current.authType != expectedAuthType || current.projectPath != expectedProjectPath) return 0
        return update(current.copy(
            alias = alias, baseUrl = baseUrl, authType = authType, instanceUuid = instanceUuid,
            projectPath = projectPath,
            cookieHost = current.cookieHost.takeIf { current.baseUrl == baseUrl && current.authType == authType },
        ))
    }

    override suspend fun saveSshConnectionIfCurrent(
        id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType,
        expectedProjectPath: String?, expectedHost: String, expectedPort: Int, expectedUser: String,
        expectedRemotePort: Int, alias: String, host: String, port: Int, user: String, remotePort: Int,
    ): Int {
        val current = getById(id) ?: return 0
        val ssh = current.ssh ?: return 0
        if (current.kind != KernelKind.SSH || current.baseUrl.isNotEmpty() || current.baseUrl != expectedUrl ||
            current.alias != expectedAlias || current.authType != expectedAuthType || current.projectPath != expectedProjectPath ||
            ssh.host != expectedHost || ssh.port != expectedPort || ssh.user != expectedUser || ssh.remotePort != expectedRemotePort) return 0
        return update(current.copy(alias = alias, ssh = ssh.copy(
            host = host, port = port, user = user, remotePort = remotePort,
            hostKeySha256 = ssh.hostKeySha256.takeIf { ssh.host == host && ssh.port == port },
        )))
    }
}
