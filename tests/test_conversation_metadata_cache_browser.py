"""Real-browser upgrade contract for the conversation metadata cache."""

from __future__ import annotations

import pytest


pytestmark = [pytest.mark.visual, pytest.mark.slow]


def test_transitional_v5_cache_upgrades_without_blocking(page):
    page.wait_for_selector("#userInput", state="visible", timeout=20_000)
    page.wait_for_function(
        "window.TofuModules?.version === 3",
        timeout=20_000,
    )

    seeded = page.evaluate(
        """async () => {
          const requestResult = (request) => new Promise((resolve, reject) => {
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error || new Error('IDB request failed'));
            request.onblocked = () => reject(new Error('IDB request blocked'));
          });
          await requestResult(indexedDB.deleteDatabase('tofu_conv_cache'));
          const openRequest = indexedDB.open('tofu_conv_cache', 5);
          openRequest.onupgradeneeded = () => {
            const database = openRequest.result;
            const metadata = database.createObjectStore(
              'conv_meta', {keyPath: 'cacheKey'},
            );
            metadata.createIndex('ownerId', 'ownerId');
            metadata.createIndex('cachedAt', 'cachedAt');
            const sidebar = database.createObjectStore(
              'sidebar_meta', {keyPath: 'cacheKey'},
            );
            sidebar.createIndex('ownerId', 'ownerId');
            sidebar.createIndex('cachedAt', 'cachedAt');
            database.createObjectStore('messages', {keyPath: 'id'});
            database.createObjectStore('conversations', {keyPath: 'id'});
          };
          const database = await requestResult(openRequest);
          const transaction = database.transaction(
            ['conv_meta', 'sidebar_meta', 'messages', 'conversations'],
            'readwrite',
          );
          transaction.objectStore('conv_meta').put({
            cacheKey: 'owner:1:conversation:transitional-v5',
            ownerId: 1, id: 'transitional-v5', title: 'legacy', cachedAt: 1,
          });
          transaction.objectStore('sidebar_meta').put({
            cacheKey: 'owner:1:conversation:transitional-v5',
            ownerId: 1, id: 'transitional-v5', title: 'legacy', cachedAt: 1,
          });
          transaction.objectStore('messages').put({id: 'legacy-transcript'});
          transaction.objectStore('conversations').put({id: 'legacy-shell'});
          await new Promise((resolve, reject) => {
            transaction.oncomplete = resolve;
            transaction.onerror = () => reject(transaction.error);
            transaction.onabort = () => reject(transaction.error);
          });
          database.close();
          return true;
        }""",
    )
    assert seeded is True

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#userInput", state="visible", timeout=20_000)
    page.wait_for_function(
        """async () => {
          const request = indexedDB.open('tofu_conv_cache');
          const database = await new Promise((resolve) => {
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => resolve(null);
          });
          if (!database) return false;
          const ready = database.version === 6;
          database.close();
          return ready;
        }""",
        timeout=20_000,
    )

    observed = page.evaluate(
        """async () => {
          const request = indexedDB.open('tofu_conv_cache');
          const database = await new Promise((resolve, reject) => {
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          const transaction = database.transaction(
            ['conv_meta', 'sidebar_meta'], 'readonly',
          );
          const metadataStore = transaction.objectStore('conv_meta');
          const sidebarStore = transaction.objectStore('sidebar_meta');
          const readAll = (store) => new Promise((resolve, reject) => {
            const read = store.getAll();
            read.onsuccess = () => resolve(read.result);
            read.onerror = () => reject(read.error);
          });
          const [metadataRows, sidebarRows] = await Promise.all([
            readAll(metadataStore), readAll(sidebarStore),
          ]);
          const result = {
            version: database.version,
            storeNames: [...database.objectStoreNames],
            metadataKeyPath: metadataStore.keyPath,
            sidebarKeyPath: sidebarStore.keyPath,
            metadataIndexes: [...metadataStore.indexNames],
            sidebarIndexes: [...sidebarStore.indexNames],
            metadataRows,
            sidebarRows,
          };
          database.close();
          return result;
        }""",
    )

    assert observed["version"] == 6
    assert set(observed["storeNames"]) == {"conv_meta", "sidebar_meta"}
    assert observed["metadataKeyPath"] == "cacheKey"
    assert observed["sidebarKeyPath"] == "cacheKey"
    assert set(observed["metadataIndexes"]) == {"cachedAt", "ownerId"}
    assert set(observed["sidebarIndexes"]) == {"cachedAt", "ownerId"}
    for row in observed["metadataRows"] + observed["sidebarRows"]:
        assert row["id"] != "transitional-v5"
        assert isinstance(row["ownerId"], int) and row["ownerId"] > 0
        assert row["cacheKey"].startswith(
            f"owner:{row['ownerId']}:conversation:"
        )

    assert not getattr(page, "_tofu_js_errors", [])
