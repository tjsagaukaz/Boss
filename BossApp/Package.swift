// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "BossApp",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/appstefan/HighlightSwift.git", from: "1.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "BossApp",
            dependencies: [
                .product(name: "HighlightSwift", package: "HighlightSwift"),
            ],
            path: "Sources"
        ),
        .testTarget(
            name: "BossAppTests",
            dependencies: ["BossApp"],
            path: "Tests"
        ),
    ]
)
