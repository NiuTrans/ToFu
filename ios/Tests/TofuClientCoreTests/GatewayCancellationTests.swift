import XCTest
@testable import TofuClientCore

final class GatewayCancellationTests: XCTestCase {
    func test_cancelled_password_response_cannot_inject_cookie_or_stamp() async {
        let rig = GatewayRig()
        await rig.store.seed(rig.profile)
        await rig.http.enqueue(makeResponse(200, body: gatewayForm()))
        await rig.http.enqueue(makeResponse(302, headers: ["set-cookie": ["code-server-session=late; Path=/"]]))
        let gate = ResponseGate()
        let arrived = expectation(description: "password request reached transport")
        await rig.http.onSend { request in
            if request.method == "POST" { arrived.fulfill(); await gate.wait() }
        }
        let login = Task { await rig.manager.login(rig.profile) }
        await fulfillment(of: [arrived], timeout: 2)
        login.cancel()
        await gate.release()
        _ = await login.value
        let writes = await rig.store.cookieHostWrites
        let injected = await rig.cookies.injected
        XCTAssertTrue(writes.isEmpty)
        XCTAssertTrue(injected.isEmpty)
    }
}

private actor ResponseGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var released = false
    func wait() async {
        if released { return }
        await withCheckedContinuation { continuation = $0 }
    }
    func release() {
        released = true
        continuation?.resume(); continuation = nil
    }
}
