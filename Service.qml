import QtQuick
import Quickshell
import Quickshell.Io

// GPU monitor engine — the shared service behind every davidjm.gpu bar
// surface.
//
// The bar renders one widget per monitor, but a single Service instance
// (enabled via the `plugins` array in shell.json) owns the one long-lived
// `gpumon.py` process and the state file it publishes. Bar widgets on every
// screen bind to `service.state`, so the number of monitors no longer
// multiplies helper processes.
//
//   bar.shell.serviceFor("davidjm.gpu")
Item {
  id: root

  // Live board state, published to every widget. Keys: present, card, util,
  // memUtil, sclkMhz, mclkMhz, tempEdge, tempJunction, tempMem, powerW,
  // fanRpm, vramUsedG, vramTotalG.
  property var state: ({})

  // Absolute path to this plugin's folder (trailing slash), resolved from the
  // QML itself so the helper is found wherever the plugin lives.
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")

  readonly property string statePath: "/tmp/davidjm-gpu-state.json"

  // One long-lived `gpumon.py` writes the state to a file every second; a
  // FileView watches it. First write lands immediately so the readout isn't
  // blank at login; restart with a debounce if the monitoring process dies.
  Process {
    id: monitorProc
    command: ["/usr/bin/python3", root.pluginDir + "gpumon.py",
              "--state-file", root.statePath]
    running: true
    onExited: monitorRestart.restart()
  }
  Timer { id: monitorRestart; interval: 3000; onTriggered: monitorProc.running = true }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onTextChanged: root.parseState(stateFile.text())
  }

  function parseState(text) {
    var json = {}
    try { json = JSON.parse(text) } catch (e) { json = {} }
    if (json.present !== undefined) root.state = json
  }

  // Middle-click "re-read": force the state FileView to re-read its file now.
  function refresh() {
    stateFile.reload()
  }

  Component.onCompleted: root.refresh()
}