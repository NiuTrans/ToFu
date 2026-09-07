// swift-tools-version: 5.9
import PackageDescription

// Pure client core: the Android session/ and data/ logic ported to
// Foundation-only Swift so every rule is testable with `swift test` on a
// macOS CI runner — no simulator, no WebKit. The iOS app target (TofuClient/,
// see project.yml) consumes this package and adds the SwiftUI/WKWebView layer.
let package = Package(
    name: "TofuClientCore",
    platforms: [.iOS(.v15), .macOS(.v12)],
    products: [
        .library(name: "TofuClientCore", targets: ["TofuClientCore"]),
    ],
    targets: [
        .target(name: "TofuClientCore", path: "Sources/TofuClientCore"),
        .testTarget(
            name: "TofuClientCoreTests",
            dependencies: ["TofuClientCore"],
            path: "Tests/TofuClientCoreTests"
        ),
    ]
)
