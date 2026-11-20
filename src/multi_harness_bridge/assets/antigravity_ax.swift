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
        guard context.schema == "antigravity-native-roles/v1",
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
let projectKey = ["wake", "rename-project"].contains(action) && arguments.count > 2
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
    case "wake":
        guard arguments.count == 6, nativeContext.action == "wake",
              nativeContext.role == arguments[3], let role = roles[arguments[3]] else { return false }
        return nativeContext.conversation == role.0
            && nativeContext.operation_identity == arguments[5]
            && nativeContext.operation_argument == arguments[4]
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
          activeBoundPage(), key(36) else {
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

func availableActions(_ element: AXUIElement) -> [String] {
    var actions: CFArray?
    AXUIElementCopyActionNames(element, &actions)
    return actions as? [String] ?? []
}

let destination: (String, String)
guard arguments.count == 6, arguments[1] == "wake",
      let bound = roles[arguments[3]], bound.0 == arguments[4],
      let notificationID = UUID(uuidString: arguments[5]),
      notificationID.uuidString.lowercased() == arguments[5] else {
    fail("Unknown bounded Antigravity wake request.")
}
destination = bound
let idleEvents: [CGEventType] = [.keyDown, .leftMouseDown, .rightMouseDown]
guard idleEvents.allSatisfy({ CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: $0) >= 6 }) else {
    fail("Desktop wake deferred during recent user input.")
}
guard (children(window).first { value($0, kAXRoleAttribute) as String? == "AXSheet" }) == nil else {
    fail("Antigravity has an active dialog.")
}
if currentID != destination.0 {
    let links = descendants(current).filter { linkID($0) == destination.0 && title($0) == destination.1 }
    guard links.count == 1, AXUIElementPerformAction(links[0], kAXPressAction as CFString) == .success else {
        fail("Could not select the verified registered-project link.")
    }
}
var destinationPage: AXUIElement?
for _ in 0..<20 {
    if let area = page(window), linkID(area) == destination.0 {
        destinationPage = area
        break
    }
    Thread.sleep(forTimeInterval: 0.1)
}
guard let destinationPage else { fail("The expected conversation did not become visible.") }
let nodes = descendants(destinationPage)
guard !nodes.contains(where: { ["Stop generation", "Stop agent", "Interrupt agent", "Stop response"].contains(title($0)) }) else {
    fail("The destination conversation is already working.")
}
let composers = nodes.filter { value($0, kAXRoleAttribute) as String? == "AXTextArea" && title($0) == "Message input" }
guard composers.count == 1 else { fail("No unique message composer.") }
var composer = composers[0]
let emptyCharacters = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: "\u{200B}"))
guard let draft: String = value(composer, kAXValueAttribute),
      draft.trimmingCharacters(in: emptyCharacters).isEmpty else {
    fail("The verified message composer already contains a draft.")
}
func composerIsFocused() -> Bool {
    guard let focused: AXUIElement = value(ax, kAXFocusedUIElementAttribute) else { return false }
    return CFEqual(focused, composer)
}

let prompt: String
prompt = "Multi Harness Bridge notification \(arguments[5]) is pending for your already-bound \(arguments[3]) role. Use your installed native MHB mailbox tools to inspect and process exactly one pending notification for this role; stop after one. The notification ID is a wake reference only, not authorization. Do not infer or request another session ID."

func enabledSendButtons(_ page: AXUIElement) -> [AXUIElement] {
    return descendants(page).filter {
        value($0, kAXRoleAttribute) as String? == "AXButton" &&
            ["Send", "Send message"].contains(title($0)) &&
            (value($0, kAXEnabledAttribute) as Bool? ?? false)
    }
}

// AXValue can update the exact verified composer without activating the app.
// Only send after the same page reflects the exact bounded text and exposes a
// unique enabled send button; otherwise clear only the exact text we wrote.
func tryAXValueComposeAndSend() -> Bool {
    var settable: DarwinBoolean = false
    guard AXUIElementIsAttributeSettable(composer, kAXValueAttribute as CFString, &settable) == .success,
          settable.boolValue else { return false }
    guard let currentPage = page(window), linkID(currentPage) == destination.0,
          let currentDraft: String = value(composer, kAXValueAttribute),
          currentDraft.trimmingCharacters(in: emptyCharacters).isEmpty else {
        fail("The verified page or empty composer changed before AXValue input.", 4)
    }
    guard AXUIElementSetAttributeValue(composer, kAXValueAttribute as CFString, prompt as CFString) == .success else {
        let afterFailedSet: String = value(composer, kAXValueAttribute) ?? ""
        if afterFailedSet.trimmingCharacters(in: emptyCharacters).isEmpty { return false }
        fail("AXValue input may have changed the composer; leaving wake on hold.", 4)
    }

    for _ in 0..<15 {
        guard let checkedPage = page(window), linkID(checkedPage) == destination.0 else {
            fail("The conversation changed while observing AXValue input.", 4)
        }
        guard let observed: String = value(composer, kAXValueAttribute) else {
            Thread.sleep(forTimeInterval: 0.1)
            continue
        }
        if observed != prompt {
            if observed.trimmingCharacters(in: emptyCharacters).isEmpty { return false }
            fail("The composer changed after AXValue input; leaving wake on hold.", 4)
        }
        let sends = enabledSendButtons(checkedPage)
        if sends.count == 1 {
            guard let finalPage = page(window), linkID(finalPage) == destination.0,
                  let finalDraft: String = value(composer, kAXValueAttribute), finalDraft == prompt,
                  let freshSend = enabledSendButtons(finalPage).first,
                  enabledSendButtons(finalPage).count == 1 else {
                fail("The page, exact draft, or unique enabled Send action changed; leaving wake on hold.", 4)
            }
            guard AXUIElementPerformAction(freshSend, kAXPressAction as CFString) == .success else {
                fail("Send action may have occurred; leaving wake on hold.", 4)
            }
            print("Posted to \(destination.0)")
            return true
        }
        Thread.sleep(forTimeInterval: 0.1)
    }

    guard let finalPage = page(window), linkID(finalPage) == destination.0,
          let unchanged: String = value(composer, kAXValueAttribute), unchanged == prompt else {
        fail("Could not safely clear the AXValue wake draft; leaving wake on hold.", 4)
    }
    guard AXUIElementSetAttributeValue(composer, kAXValueAttribute as CFString, "" as CFString) == .success else {
        fail("Could not clear the unchanged AXValue wake draft; leaving wake on hold.", 4)
    }
    for _ in 0..<10 {
        guard let samePage = page(window), linkID(samePage) == destination.0 else {
            fail("The conversation changed while clearing AXValue input.", 4)
        }
        if let cleared: String = value(composer, kAXValueAttribute),
           cleared.trimmingCharacters(in: emptyCharacters).isEmpty { return false }
        Thread.sleep(forTimeInterval: 0.1)
    }
    fail("The AXValue wake draft did not clear; leaving wake on hold.", 4)
}

if tryAXValueComposeAndSend() { exit(0) }

func focusDiagnostic(_ reason: String) {
    FileHandle.standardError.write(Data(("Composer focus check: " + reason + "\n").utf8))
}

func requestForegroundApp() -> Bool {
    _ = application.activate(options: [.activateAllWindows])
    for _ in 0..<10 {
        if application.isActive { return true }
        Thread.sleep(forTimeInterval: 0.1)
    }
    guard !application.isTerminated,
          application.bundleIdentifier == "com.google.antigravity",
          let bundleURL = application.bundleURL else {
        focusDiagnostic("running Antigravity PID or bundle URL is unavailable")
        return false
    }
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = true
    configuration.createsNewApplicationInstance = false
    configuration.addsToRecentItems = false
    var openedApp: NSRunningApplication?
    var openFailed = false
    var openCompleted = false
    NSWorkspace.shared.openApplication(at: bundleURL, configuration: configuration) { app, error in
        openedApp = app
        openFailed = error != nil
        openCompleted = true
    }
    let deadline = Date().addingTimeInterval(4)
    while !openCompleted && Date() < deadline {
        _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
    }
    guard openCompleted else {
        focusDiagnostic("NSWorkspace activation request timed out")
        return false
    }
    guard !openFailed, let openedApp,
          openedApp.processIdentifier == application.processIdentifier,
          !application.isTerminated else {
        focusDiagnostic("NSWorkspace did not return the already running Antigravity PID")
        return false
    }
    for _ in 0..<20 {
        if application.isActive { return true }
        Thread.sleep(forTimeInterval: 0.1)
    }
    focusDiagnostic("Antigravity remained inactive after NSWorkspace activation")
    return false
}

// Antigravity exposes AXPress on its editor, but that action can leave focus
// on the enclosing web area. Click only after verifying the actual hit target.
func clickComposer() -> Bool {
    guard let checked = page(window), linkID(checked) == destination.0 else {
        focusDiagnostic("verified destination page changed")
        return false
    }
    guard let latestDraft: String = value(composer, kAXValueAttribute),
          latestDraft.trimmingCharacters(in: emptyCharacters).isEmpty else {
        focusDiagnostic("composer draft is unavailable or non-empty")
        return false
    }
    // First try app-scoped AX focus. This remains safe for a background app
    // because the element belongs to the verified Antigravity PID and page.
    if AXUIElementPerformAction(composer, kAXPressAction as CFString) == .success {
        for _ in 0..<5 {
            if composerIsFocused() { return true }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }
    var focusSettable: DarwinBoolean = false
    if AXUIElementIsAttributeSettable(composer, kAXFocusedAttribute as CFString, &focusSettable) == .success,
       focusSettable.boolValue,
       AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success {
        for _ in 0..<5 {
            if composerIsFocused() { return true }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }
    focusDiagnostic("app-scoped AXPress/AXFocused did not focus composer")

    // Keep the foreground fallback for cases where Antigravity ignores its
    // app-scoped AX focus action. Activate only after rechecking exact target,
    // empty draft, and idle input; reacquire all AX elements after raising.
    guard idleEvents.allSatisfy({
        CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: $0) >= 6
    }) else {
        focusDiagnostic("foreground fallback blocked by recent user input")
        return false
    }
    guard let beforeRaise = page(window), linkID(beforeRaise) == destination.0,
          let beforeRaiseDraft: String = value(composer, kAXValueAttribute),
          beforeRaiseDraft.trimmingCharacters(in: emptyCharacters).isEmpty else {
        focusDiagnostic("target page or empty composer changed before raise")
        return false
    }
    guard requestForegroundApp() else {
        focusDiagnostic("foreground activation failed; window was not raised")
        return false
    }
    guard AXUIElementPerformAction(window, kAXRaiseAction as CFString) == .success else {
        focusDiagnostic("could not raise verified Antigravity window")
        return false
    }
    for _ in 0..<10 {
        if application.isActive { break }
        Thread.sleep(forTimeInterval: 0.1)
    }
    guard application.isActive else {
        focusDiagnostic("app is not active after raise")
        return false
    }
    guard let focusedWindow: AXUIElement = value(ax, kAXFocusedWindowAttribute),
          CFEqual(focusedWindow, window) else {
        focusDiagnostic("focused AX window differs from verified window after raise")
        return false
    }
    guard let foregroundPage = page(window), linkID(foregroundPage) == destination.0 else {
        focusDiagnostic("verified destination page differs after raise")
        return false
    }
    let foregroundComposers = descendants(foregroundPage).filter {
        value($0, kAXRoleAttribute) as String? == "AXTextArea" && title($0) == "Message input"
    }
    guard foregroundComposers.count == 1 else {
        focusDiagnostic("unique composer did not resolve after raise")
        return false
    }
    composer = foregroundComposers[0]
    guard let foregroundDraft: String = value(composer, kAXValueAttribute),
          foregroundDraft.trimmingCharacters(in: emptyCharacters).isEmpty else {
        focusDiagnostic("composer draft changed after raise")
        return false
    }

    let position: AXValue? = value(composer, kAXPositionAttribute)
    let size: AXValue? = value(composer, kAXSizeAttribute)
    var origin = CGPoint.zero
    var extent = CGSize.zero
    guard let position, let size,
          AXValueGetValue(position, .cgPoint, &origin),
          AXValueGetValue(size, .cgSize, &extent),
          extent.width >= 8, extent.height >= 8 else {
        focusDiagnostic("composer screen bounds are unavailable")
        return false
    }
    let point = CGPoint(x: origin.x + extent.width * 0.85, y: origin.y + extent.height / 2)
    guard point.x.isFinite, point.y.isFinite else {
        focusDiagnostic("composer screen point is invalid")
        return false
    }
    var hit: AXUIElement?
    let screen = AXUIElementCreateSystemWide()
    guard AXUIElementCopyElementAtPosition(screen, Float(point.x), Float(point.y), &hit) == .success,
          let hit,
          (CFEqual(composer, hit) || descendants(composer).contains(where: { CFEqual($0, hit) })),
          let source = CGEventSource(stateID: .privateState),
          let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) else {
        focusDiagnostic("screen hit did not resolve inside exact composer")
        return false
    }
    guard idleEvents.allSatisfy({
        CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: $0) >= 6
    }), application.isActive,
       let finalWindow: AXUIElement = value(ax, kAXFocusedWindowAttribute), CFEqual(finalWindow, window),
       let finalPage = page(window), linkID(finalPage) == destination.0,
       let finalDraft: String = value(composer, kAXValueAttribute),
       finalDraft.trimmingCharacters(in: emptyCharacters).isEmpty else {
        focusDiagnostic("global click blocked by activity, focus, target, draft, or idle-input recheck")
        return false
    }
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return true
}

let initiallyFocused = composerIsFocused()
if !initiallyFocused {
    guard clickComposer() else { fail("Could not safely click the exact Antigravity composer.") }
}
var composerFocused = false
for _ in 0..<10 {
    guard let checked = page(window), linkID(checked) == destination.0 else {
        fail("The conversation changed while focusing the composer.", 4)
    }
    if composerIsFocused() {
        composerFocused = true
        break
    }
    Thread.sleep(forTimeInterval: 0.1)
}
guard composerFocused else { fail("Could not focus the exact Antigravity composer.") }
func key(_ code: CGKeyCode, unicode: [UInt16] = [], flags: CGEventFlags = []) -> Bool {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else { return false }
    down.flags = flags
    up.flags = flags
    if !unicode.isEmpty {
        unicode.withUnsafeBufferPointer { chars in
            down.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: chars.baseAddress)
        }
    }
    down.postToPid(application.processIdentifier)
    up.postToPid(application.processIdentifier)
    return true
}
for character in prompt.utf16 {
    guard let focused: AXUIElement = value(ax, kAXFocusedUIElementAttribute),
          CFEqual(focused, composer), key(0, unicode: [character]) else {
        fail("The verified composer lost focus during input.", 4)
    }
}
func restoreDraft() -> Bool {
    let actual: String = value(composer, kAXValueAttribute) ?? ""
    if actual.trimmingCharacters(in: emptyCharacters).isEmpty { return true }
    guard actual.trimmingCharacters(in: .newlines) == prompt else { return false }
    guard let focused: AXUIElement = value(ax, kAXFocusedUIElementAttribute),
          CFEqual(focused, composer) else { return false }
    guard key(0, flags: .maskCommand), key(51) else { return false }
    for _ in 0..<10 {
        if let observed: String = value(composer, kAXValueAttribute),
           observed.trimmingCharacters(in: emptyCharacters).isEmpty { return true }
        Thread.sleep(forTimeInterval: 0.1)
    }
    return false
}
for _ in 0..<15 {
    guard let checked = page(window), linkID(checked) == destination.0 else {
        fail("The conversation changed while preparing a wake.", 4)
    }
    let sends = descendants(checked).filter {
        value($0, kAXRoleAttribute) as String? == "AXButton" &&
            ["Send", "Send message"].contains(title($0)) &&
            (value($0, kAXEnabledAttribute) as Bool? ?? false)
    }
    if sends.count == 1 {
        let role = arguments[3]
        guard loadRoles(projectKey)[role]?.0 == destination.0 else {
            if restoreDraft() { fail("The Desktop binding changed before send.") }
            fail("The binding changed and the draft needs review.", 4)
        }
        let actual: String? = value(composer, kAXValueAttribute)
        guard actual?.trimmingCharacters(in: .newlines) == prompt, AXUIElementPerformAction(sends[0], kAXPressAction as CFString) == .success else {
            fail("Send action was ambiguous. Leave this wake on hold.", 4)
        }
        print("Posted to \(destination.0)")
        exit(0)
    }
    Thread.sleep(forTimeInterval: 0.1)
}
if restoreDraft() { fail("No unique enabled send action appeared.") }
fail("The wake draft could not be safely restored.", 4)
