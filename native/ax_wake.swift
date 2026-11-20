// Generic macOS GUI sender for apps that expose their session id in AX text.
// Build with: swiftc native/ax_wake.swift -framework AppKit -framework ApplicationServices
// This deliberately refuses hidden/ambiguous destinations and non-empty composers.
import AppKit
import ApplicationServices
import Foundation

func fail(_ message: String, _ code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}
func attr<T>(_ e: AXUIElement, _ name: String) -> T? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(e, name as CFString, &value) == .success else { return nil }
    return value as? T
}
func descendants(_ root: AXUIElement, depth: Int = 0) -> [AXUIElement] {
    if depth > 16 { return [] }
    let children: [AXUIElement] = attr(root, kAXChildrenAttribute) ?? []
    return children + children.flatMap { descendants($0, depth: depth + 1) }
}
guard CommandLine.arguments.count == 5 else {
    fail("usage: ax_wake <bundle-id> <exact-window-title> <session-id> <wake-text>")
}
let bundleID = CommandLine.arguments[1]
let expectedTitle = CommandLine.arguments[2]
let expectedSession = CommandLine.arguments[3]
let message = CommandLine.arguments[4]
guard !bundleID.isEmpty, !expectedTitle.isEmpty, !expectedSession.isEmpty, !message.isEmpty else {
    fail("all target and message values are required")
}
guard AXIsProcessTrusted() else { fail("Grant Accessibility access to this helper in macOS Privacy & Security settings") }
let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
guard apps.count == 1, let app = apps.first, !app.isTerminated else { fail("target app is absent or ambiguous") }
let ax = AXUIElementCreateApplication(app.processIdentifier)
AXUIElementSetMessagingTimeout(ax, 1.0)
let windows: [AXUIElement] = attr(ax, kAXWindowsAttribute) ?? []
let matches = windows.filter { (attr($0, kAXTitleAttribute) as String?) == expectedTitle }
guard matches.count == 1, let window = matches.first else { fail("exact destination window is absent or ambiguous") }
let nodes = descendants(window)
let visibleIdentity = nodes.compactMap { (attr($0, kAXValueAttribute) as String?) }
    .joined(separator: "\n")
guard visibleIdentity.contains(expectedSession) else { fail("session identity is not visible in the selected window") }
let editors = nodes.filter {
    let role: String? = attr($0, kAXRoleAttribute)
    return role == kAXTextAreaRole || role == kAXTextFieldRole
}
guard editors.count == 1, let editor = editors.first else { fail("composer is absent or ambiguous") }
let current: String = attr(editor, kAXValueAttribute) ?? ""
guard current.isEmpty else { fail("composer is not empty; refusing to replace user text") }
guard AXUIElementSetAttributeValue(editor, kAXValueAttribute as CFString, message as CFString) == .success else {
    fail("could not set the wake draft")
}
let written: String? = attr(editor, kAXValueAttribute)
guard written == message else { fail("wake draft did not match exactly; refusing to submit", 4) }
let fresh = descendants(window)
let buttons = fresh.filter {
    let role: String? = attr($0, kAXRoleAttribute)
    let title: String? = attr($0, kAXTitleAttribute)
    let value: String? = attr($0, kAXValueAttribute)
    return role == kAXButtonRole && ["Send", "Submit"].contains(title ?? value ?? "")
}
guard buttons.count == 1, AXUIElementPerformAction(buttons[0], kAXPressAction as CFString) == .success else {
    _ = AXUIElementSetAttributeValue(editor, kAXValueAttribute as CFString, "" as CFString)
    fail("unique Send action unavailable; wake was not submitted")
}
