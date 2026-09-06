import Foundation
import SQLite3

/// Store-layer failure.
public enum TofuStoreError: Error, Equatable {
    case openFailed(String)
    case execFailed(String)
}

/// SQLite-backed profile store — the port of Android's Room ProfileDatabase:
/// table `servers` with the same columns, the same `lastUsedAt DESC`
/// ordering, and the MIGRATION_1_2 `projectPath` add-column applied to
/// databases written by an older build. Direct sqlite3 C API: one table,
/// explicit SQL, no ORM — the schema stays single-source with Entities.kt.
public final class SQLiteProfileStore: ProfileStore, @unchecked Sendable {

    private static let columns =
        "id, alias, instanceUuid, baseUrl, authType, cookieHost, lastUsedAt, projectPath"
    private static let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    private var db: OpaquePointer?
    private let lock = NSLock()
    /// App-layer hook invoked after any mutation — the Room Flow analogue
    /// that lets the SwiftUI list re-render.
    public var onDidChange: (@Sendable () -> Void)?

    public init(path: String) throws {
        if sqlite3_open(path, &db) != SQLITE_OK {
            let message = db.flatMap { String(cString: sqlite3_errmsg($0)) } ?? "unknown"
            sqlite3_close(db)
            db = nil
            throw TofuStoreError.openFailed(message)
        }
        try exec("""
            CREATE TABLE IF NOT EXISTS servers(
                id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                alias TEXT NOT NULL,
                instanceUuid TEXT,
                baseUrl TEXT NOT NULL,
                authType TEXT NOT NULL,
                cookieHost TEXT,
                lastUsedAt INTEGER NOT NULL,
                projectPath TEXT
            )
            """)
        // MIGRATION_1_2: databases created at schema version 1 lack the
        // supervisor's project path column.
        if !hasColumn("projectPath", inTable: "servers") {
            try exec("ALTER TABLE servers ADD COLUMN projectPath TEXT")
        }
    }

    deinit { sqlite3_close(db) }

    // MARK: - ProfileStore (sync bodies satisfy the async requirements)

    public func getAllOnce() -> [Profile] {
        lock.lock()
        defer { lock.unlock() }
        return readRows(
            "SELECT \(Self.columns) FROM servers ORDER BY lastUsedAt DESC"
        )
    }

    public func getById(_ id: Int64) -> Profile? {
        lock.lock()
        defer { lock.unlock() }
        return readRows("SELECT \(Self.columns) FROM servers WHERE id = ?") { stmt in
            sqlite3_bind_int64(stmt, 1, id)
        }.first
    }

    public func getByAlias(_ alias: String) -> Profile? {
        lock.lock()
        defer { lock.unlock() }
        return readRows("SELECT \(Self.columns) FROM servers WHERE alias = ?") { stmt in
            bindText(stmt, 1, alias)
        }.first
    }

    @discardableResult
    public func insert(_ profile: Profile) -> Int64 {
        lock.lock()
        var stmt: OpaquePointer?
        let sql = """
            INSERT INTO servers(alias, instanceUuid, baseUrl, authType, cookieHost, lastUsedAt, projectPath)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            bindText(stmt, 1, profile.alias)
            bindText(stmt, 2, profile.instanceUuid)
            bindText(stmt, 3, profile.baseUrl)
            bindText(stmt, 4, profile.authType.rawValue)
            bindText(stmt, 5, profile.cookieHost)
            sqlite3_bind_int64(stmt, 6, profile.lastUsedAt)
            bindText(stmt, 7, profile.projectPath)
            sqlite3_step(stmt)
        }
        sqlite3_finalize(stmt)
        let id = sqlite3_last_insert_rowid(db)
        lock.unlock()
        notify()
        return id
    }

    public func update(_ profile: Profile) {
        lock.lock()
        var stmt: OpaquePointer?
        let sql = """
            UPDATE servers SET alias=?, instanceUuid=?, baseUrl=?, authType=?,
                cookieHost=?, lastUsedAt=?, projectPath=? WHERE id=?
            """
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            bindText(stmt, 1, profile.alias)
            bindText(stmt, 2, profile.instanceUuid)
            bindText(stmt, 3, profile.baseUrl)
            bindText(stmt, 4, profile.authType.rawValue)
            bindText(stmt, 5, profile.cookieHost)
            sqlite3_bind_int64(stmt, 6, profile.lastUsedAt)
            bindText(stmt, 7, profile.projectPath)
            sqlite3_bind_int64(stmt, 8, profile.id)
            sqlite3_step(stmt)
        }
        sqlite3_finalize(stmt)
        lock.unlock()
        notify()
    }

    public func deleteById(_ id: Int64) {
        mutate("DELETE FROM servers WHERE id = ?") { stmt in
            sqlite3_bind_int64(stmt, 1, id)
        }
    }

    public func touchLastUsed(_ id: Int64, _ at: Int64) {
        mutate("UPDATE servers SET lastUsedAt = ? WHERE id = ?") { stmt in
            sqlite3_bind_int64(stmt, 1, at)
            sqlite3_bind_int64(stmt, 2, id)
        }
    }

    public func setAuthType(_ id: Int64, _ authType: AuthType) {
        mutate("UPDATE servers SET authType = ? WHERE id = ?") { stmt in
            bindText(stmt, 1, authType.rawValue)
            sqlite3_bind_int64(stmt, 2, id)
        }
    }

    public func setCookieHost(_ id: Int64, _ host: String?) {
        mutate("UPDATE servers SET cookieHost = ? WHERE id = ?") { stmt in
            bindText(stmt, 1, host)
            sqlite3_bind_int64(stmt, 2, id)
        }
    }

    // MARK: - internals

    private func mutate(_ sql: String, bind: (OpaquePointer?) -> Void) {
        lock.lock()
        var stmt: OpaquePointer?
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            bind(stmt)
            sqlite3_step(stmt)
        }
        sqlite3_finalize(stmt)
        lock.unlock()
        notify()
    }

    private func readRows(
        _ sql: String,
        bind: ((OpaquePointer?) -> Void)? = nil
    ) -> [Profile] {
        var stmt: OpaquePointer?
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        bind?(stmt)
        var rows: [Profile] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            rows.append(Profile(
                id: sqlite3_column_int64(stmt, 0),
                alias: text(stmt, 1) ?? "",
                instanceUuid: text(stmt, 2),
                baseUrl: text(stmt, 3) ?? "",
                authType: AuthType(rawValue: text(stmt, 4) ?? "") ?? .none,
                cookieHost: text(stmt, 5),
                lastUsedAt: sqlite3_column_int64(stmt, 6),
                projectPath: text(stmt, 7)
            ))
        }
        return rows
    }

    private func exec(_ sql: String) throws {
        if sqlite3_exec(db, sql, nil, nil, nil) != SQLITE_OK {
            throw TofuStoreError.execFailed(String(cString: sqlite3_errmsg(db)))
        }
    }

    private func hasColumn(_ name: String, inTable table: String) -> Bool {
        var stmt: OpaquePointer?
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(\(table))", -1, &stmt, nil) == SQLITE_OK
        else { return false }
        while sqlite3_step(stmt) == SQLITE_ROW {
            if let c = sqlite3_column_text(stmt, 1), String(cString: c) == name { return true }
        }
        return false
    }

    private func bindText(_ stmt: OpaquePointer?, _ index: Int32, _ value: String?) {
        if let value {
            sqlite3_bind_text(stmt, index, (value as NSString).utf8String, -1, Self.transient)
        } else {
            sqlite3_bind_null(stmt, index)
        }
    }

    private func text(_ stmt: OpaquePointer?, _ index: Int32) -> String? {
        guard sqlite3_column_type(stmt, index) != SQLITE_NULL,
              let c = sqlite3_column_text(stmt, index) else { return nil }
        return String(cString: c)
    }

    private func notify() {
        guard let handler = onDidChange else { return }
        DispatchQueue.main.async { handler() }
    }
}
