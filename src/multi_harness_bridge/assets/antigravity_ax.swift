import ApplicationServices
import AppKit
import Darwin

let arguments = CommandLine.arguments
let roleNames: Set<String> = ["coordinator", "inbox"]

func fail(_ reason: String, _ code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data((reason + "\n").utf8))
    exit(code)
}

struct NativeRole: Decodable {
    let conversation: String
    let title: String
    let project_id: String
}

struct NativeRoleContext: Decodable {
    let schema: String
    let created_at: Double
    let project_key: String
    let action: String
    let role: String?
    let conversation: String?
    let requested_title: String?
    let operation_identity: String?
    let operation_argument: String?
    let roles: [String: [String: NativeRole]]
}

// launchd's AX helper no longer opens Antigravity's private SQLite database.
// The already-authorized Python launcher validates those native records and
// leaves this short-lived, owner-only context beside its one-shot plist.
var executablePathBuffer = [CChar](repeating: 0, count: 4096)
guard proc_pidpath(getpid(), &executablePathBuffer, UInt32(executablePathBuffer.count)) > 0 else {
    fail("Unable to resolve the Desktop helper executable path.")
}
let executableURL = URL(fileURLWithPath: String(cString: executablePathBuffer))
    .standardizedFileURL.resolvingSymlinksInPath()
let stateDirectory = executableURL.deletingLastPathComponent()

func loadNativeContext() -> NativeRoleContext {
    guard let contextPath = ProcessInfo.processInfo.environment["ANTIGRAVITY_AX_NATIVE_CONTEXT"] else {
        fail("A vetted native role context from the Python launcher is required.")
    }
    let contextURL = URL(fileURLWithPath: contextPath).standardizedFileURL.resolvingSymlinksInPath()
    guard contextURL.path.hasPrefix(stateDirectory.path + "/") else {
        fail("The vetted native role context is outside bridge state.")
    }
    do {
        let attributes = try FileManager.default.attributesOfItem(atPath: contextURL.path)
        let owner = (attributes[.ownerAccountID] as? NSNumber)?.intValue
        let mode = (attributes[.posixPermissions] as? NSNumber)?.intValue
        let modified = attributes[.modificationDate] as? Date
        guard owner == Int(getuid()), mode == 0o600, let modified,
              abs(Date().timeIntervalSince(modified)) <= 30 else {
            fail("The vetted native role context is stale or has unsafe ownership or permissions.")
        }
        let data = try Data(contentsOf: contextURL)
        let context = try JSONDecoder().decode(NativeRoleContext.self, from: data)
        guard context.schema == "antigravity-native-roles/v2",
              abs(Date().timeIntervalSince1970 - context.created_at) <= 30,
              !context.project_key.isEmpty,
              context.roles[context.project_key] != nil else {
            fail("The vetted native role context is invalid or expired.")
        }
        var seenConversations = Set<String>()
        var projectIDs: [String: String] = [:]
        for (key, roles) in context.roles {
            guard !key.isEmpty, !roles.isEmpty, Set(roles.keys).isSubset(of: roleNames),
                  let projectID = roles.values.first?.project_id,
                  UUID(uuidString: projectID)?.uuidString.lowercased() == projectID,
                  roles.values.allSatisfy({ $0.project_id == projectID }) else {
                fail("The vetted native role set is invalid.")
            }
            if let existing = projectIDs[projectID], existing != key {
                fail("Native project identifiers collide across registered workspaces.")
            }
            projectIDs[projectID] = key
            for role in roles.values {
                guard UUID(uuidString: role.conversation)?.uuidString.lowercased() == role.conversation,
                      !role.title.isEmpty, seenConversations.insert(role.conversation).inserted else {
                    fail("The vetted native role membership is invalid or duplicated.")
                }
            }
        }
        guard !seenConversations.isEmpty else { fail("No verified native Desktop role is available.") }
        return context
    } catch {
        fail("Unable to read the vetted native role context: \(type(of: error)).")
    }
}

let nativeContext = loadNativeContext()
let action = arguments.count > 1 ? arguments[1] : ""
let projectKey = action == "rename-project" && arguments.count > 2
    ? arguments[2] : nativeContext.project_key
guard nativeContext.project_key == projectKey else {
    fail("The vetted native role context is for a different project.")
}
guard let rolesForProject = nativeContext.roles[projectKey] else {
    fail("Unknown Antigravity project.")
}
let projectIDs = Set(rolesForProject.values.map(\.project_id))
guard projectIDs.count == 1, let project = projectIDs.first else {
    fail("The native project identifier is ambiguous.")
}

func loadRoles(_ key: String) -> [String: (String, String)] {
    guard let nativeRoles = nativeContext.roles[key] else { fail("No vetted roles exist for this project.") }
    return nativeRoles.mapValues { ($0.conversation, $0.title) }
}

let roles = loadRoles(projectKey)
let boundConversationIDs = Set(roles.values.map { $0.0 })

func contextMatchesInvocation() -> Bool {
    guard arguments.count > 1 else { return false }
    switch arguments[1] {
    case "verify-bindings":
        return arguments.count == 2 && nativeContext.action == "verify-bindings"
    case "rename-project":
        guard arguments.count == 6, nativeContext.action == "rename-project",
              nativeContext.role == arguments[3], nativeContext.conversation == arguments[4],
              nativeContext.requested_title == arguments[5],
              nativeContext.operation_identity == arguments[4],
              nativeContext.operation_argument == arguments[5],
              let role = roles[arguments[3]] else { return false }
        return role.0 == arguments[4]
    default:
        return false
    }
}

guard contextMatchesInvocation() else {
    fail("The one-shot native role context does not match this helper invocation.")
}

func value<T>(_ element: AXUIElement, _ name: String) -> T? {
    var result: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &result) == .success else { return nil }
    return result as? T
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    return value(element, kAXChildrenAttribute) ?? []
}

func availableActions(_ element: AXUIElement) -> [String] {
    var actions: CFArray?
    AXUIElementCopyActionNames(element, &actions)
    return actions as? [String] ?? []
}

func submitTitle(_ pid: pid_t) -> Bool {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 36, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 36, keyDown: false) else { return false }
    down.postToPid(pid)
    up.postToPid(pid)
    return true
}

func descendants(_ root: AXUIElement) -> [AXUIElement] {
    var pending = [root]
    var found: [AXUIElement] = []
    while let node = pending.popLast(), found.count < 5000 {
        found.append(node)
        pending.append(contentsOf: children(node).reversed())
    }
    return found
}

func linkID(_ node: AXUIElement) -> String? {
    let url: URL? = value(node, kAXURLAttribute)
    guard let url, url.host == "127.0.0.1" || url.host == "localhost",
          url.pathComponents.count == 3, url.pathComponents[1] == "c" else { return nil }
    return url.lastPathComponent
}

func page(_ window: AXUIElement) -> AXUIElement? {
    return descendants(window).first { (node: AXUIElement) in
        let role: String? = value(node, kAXRoleAttribute)
        guard role == "AXWebArea", linkID(node) != nil,
              let url: URL = value(node, kAXURLAttribute),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        let visibleProject = parts.queryItems?.first(where: { $0.name == "project" })?.value
        return visibleProject == project
    }
}

func title(_ element: AXUIElement) -> String {
    let description: String? = value(element, kAXDescriptionAttribute)
    let name: String? = value(element, kAXTitleAttribute)
    return description.flatMap { $0.isEmpty ? nil : $0 } ?? name ?? ""
}

if arguments == [arguments[0], "verify-bindings"] {
    print("Validated native project membership for \(boundConversationIDs.count) bound Desktop conversations.")
    exit(0)
}

guard AXIsProcessTrusted() else { fail("macOS Accessibility authorization is required.", 3) }
let applications = NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.antigravity")
// Production and disposable profiles may be separate processes with the same bundle ID.
var candidates: [(NSRunningApplication, AXUIElement, AXUIElement)] = []
for app in applications {
    let root = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(root, 0.5)
    guard let windows: [AXUIElement] = value(root, kAXWindowsAttribute) else {
        fail("Unable to inspect every running Antigravity Desktop profile.")
    }
    for window in windows where page(window) != nil {
        candidates.append((app, root, window))
    }
}
guard candidates.count == 1, let (application, ax, window) = candidates.first,
      let current = page(window), let currentID = linkID(current),
      boundConversationIDs.contains(currentID) else {
    fail("Leave one Antigravity window on a bound chat for this registered project.")
}

// Rename only the visible bound conversation, after the role transfer has succeeded.
if arguments.count == 6, arguments[1] == "rename-project" {
    let role = arguments[3], expected = arguments[4], requested = arguments[5]
    let activeWindow: AXUIElement? = value(ax, kAXFocusedWindowAttribute)
    guard roles[role]?.0 == expected, currentID == expected,
          let activeWindow, CFEqual(window, activeWindow),
          !requested.isEmpty, requested.count <= 240,
          requested.rangeOfCharacter(from: .controlCharacters) == nil else {
        fail("The visible bound conversation does not match the requested title change.")
    }
    func activeBoundPage() -> Bool {
        guard let focusedWindow: AXUIElement = value(ax, kAXFocusedWindowAttribute),
              CFEqual(window, focusedWindow),
              let activePage = page(window) else { return false }
        return linkID(activePage) == expected
    }
    func action(_ label: String) -> Bool {
        let elements = descendants(window).filter {
            title($0) == label && availableActions($0).contains(kAXPressAction as String)
        }
        return elements.count == 1 && AXUIElementPerformAction(elements[0], kAXPressAction as CFString) == .success
    }
    func waitUntil(_ predicate: () -> Bool) -> Bool {
        for _ in 0..<30 {
            if predicate() { return true }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return false
    }
    func isTitleField(_ node: AXUIElement) -> Bool {
        let role: String? = value(node, kAXRoleAttribute)
        guard ["AXTextField", "AXTextArea", "AXComboBox"].contains(role ?? "") else { return false }
        let description: String? = value(node, kAXDescriptionAttribute)
        let placeholder: String? = value(node, "AXPlaceholderValue")
        return description == "Edit title" || placeholder == "Edit title" || title(node) == "Edit title"
    }
    func titleField() -> AXUIElement? {
        let fields = descendants(window).filter(isTitleField)
        return fields.count == 1 ? fields[0] : nil
    }
    func focusedTitleField(with expectedValue: String) -> AXUIElement? {
        guard activeBoundPage(),
              let focused: AXUIElement = value(ax, kAXFocusedUIElementAttribute),
              isTitleField(focused),
              let currentValue: String = value(focused, kAXValueAttribute),
              currentValue == expectedValue else { return nil }
        return focused
    }
    func sidebarHasRequestedTitle() -> Bool {
        guard activeBoundPage() else { return false }
        return descendants(window).contains { linkID($0) == expected && title($0) == requested }
    }

    // A previous invocation may already have committed the title, or may
    // have opened the editor and left it focused. Handle both without opening
    // another menu against a changed accessibility tree.
    if sidebarHasRequestedTitle() {
        print("Verified sidebar title.")
        exit(0)
    }
    var field = titleField()
    if field == nil {
        guard action("More actions"), waitUntil({ action("Rename") }),
              waitUntil({ titleField() != nil }) else {
            fail("Could not open the native Desktop title editor.")
        }
        field = titleField()
    }
    guard activeBoundPage(), let editor = field else {
        fail("The active Desktop title editor does not belong to the bound conversation.")
    }
    let existingValue: String = value(editor, kAXValueAttribute) ?? ""
    if existingValue != requested {
        guard AXUIElementSetAttributeValue(editor, kAXValueAttribute as CFString, requested as CFString) == .success else {
            fail("Could not set the native Desktop title value.")
        }
    }
    // AXSetValue can rerender the web view and invalidate the element proxy.
    // Resolve the focused editor again from the active page and exact role,
    // description, and value instead of comparing against the stale proxy.
    guard waitUntil({ focusedTitleField(with: requested) != nil }),
          let verifiedEditor = focusedTitleField(with: requested),
          activeBoundPage(), submitTitle(application.processIdentifier) else {
        fail("Could not verify the focused native Desktop title editor.")
    }
    _ = verifiedEditor
    guard waitUntil({ sidebarHasRequestedTitle() }) else {
        fail("Could not verify the requested sidebar title.")
    }
    print("Verified sidebar title.")
    exit(0)
}

if arguments == [arguments[0], "probe"] {
    print("Trusted Accessibility connection to bound registered-project Desktop window: \(currentID)")
    for node in descendants(current) {
        let role: String? = value(node, kAXRoleAttribute)
        let name = title(node)
        if role == "AXTextArea" || name == "Send message" || roles.values.contains(where: { $0.1 == name }) {
            let actions = availableActions(node)
            let identifier = linkID(node) ?? ""
            var settable: DarwinBoolean = false
            AXUIElementIsAttributeSettable(node, kAXValueAttribute as CFString, &settable)
            print(role ?? "", name, identifier, actions, "value settable:", settable.boolValue)

        }
    }
    exit(0)
}

fail("Unknown Antigravity helper action.")
