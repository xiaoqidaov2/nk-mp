#!/usr/bin/env node
// CLI tool for WMPFDebugger - list and attach to CDP targets

import { CDPClient, formatTargetList, findMiniAppTargets, findBrowserTargets, filterTargetsByType, CDPTarget } from "./cdp-targets";

const DEFAULT_CDP_PORT = 62000;
const DEFAULT_CDP_HOST = "127.0.0.1";

interface CLIOptions {
    host: string;
    port: number;
    command: "list" | "attach" | "auto-attach" | "watch";
    targetId?: string;
    type?: string;
    pattern?: string;
    autoAttach?: boolean;
}

function printUsage() {
    console.log(`
WMPFDebugger CLI - CDP Target Management

Usage:
  wmpf-cli [options] [command]

Commands:
  list                    List all available CDP targets
  attach <targetId>       Attach to a specific target by ID
  auto-attach [type]      Automatically attach to first available target
  watch                   Watch for new targets and auto-attach

Options:
  --host <hostname>       CDP host address (default: 127.0.0.1)
  --port <port>           CDP port (default: 62000)
  --type <type>           Filter targets by type (page, iframe, webview, etc.)
  --pattern <pattern>     Filter targets by title pattern (regex)
  --auto                  Auto-attach to matching target

Examples:
  wmpf-cli list                              List all targets
  wmpf-cli list --type page                  List only page targets
  wmpf-cli attach ABC123                  Attach to specific target
  wmpf-cli auto-attach page                  Auto-attach to first page
  wmpf-cli auto-attach --pattern "小程序"    Auto-attach to miniapp
  wmpf-cli watch                             Watch and auto-attach to new targets

Environment Variables:
  WMPF_CDP_PORT       CDP port (default: 62000)
  WMPF_CDP_HOST       CDP host (default: 127.0.0.1)
  WMPF_DEBUG          Enable debug logging
`);
}

function parseArgs(args: string[]): CLIOptions {
    const options: CLIOptions = {
        host: process.env.WMPF_CDP_HOST || DEFAULT_CDP_HOST,
        port: parseInt(process.env.WMPF_CDP_PORT || String(DEFAULT_CDP_PORT)),
        command: "list"
    };

    let i = 0;
    while (i < args.length) {
        const arg = args[i];

        switch (arg) {
            case "--help":
            case "-h":
                printUsage();
                process.exit(0);
                break;

            case "--host":
                options.host = args[++i];
                break;

            case "--port":
                options.port = parseInt(args[++i]);
                break;

            case "--type":
                options.type = args[++i];
                break;

            case "--pattern":
                options.pattern = args[++i];
                break;

            case "--auto":
                options.autoAttach = true;
                break;

            case "list":
            case "attach":
            case "auto-attach":
            case "watch":
                options.command = arg;
                break;

            default:
                if (options.command === "attach" && !options.targetId) {
                    options.targetId = arg;
                }
                break;
        }

        i++;
    }

    return options;
}

async function commandList(client: CDPClient, options: CLIOptions): Promise<void> {
    let targets = await client.getTargets();

    // Apply filters
    if (options.type) {
        targets = filterTargetsByType(targets, options.type);
    }

    if (options.pattern) {
        const regex = new RegExp(options.pattern, "i");
        targets = targets.filter(t => regex.test(t.title) || regex.test(t.url));
    }

    console.log(formatTargetList(targets));

    // Group by type for summary
    const byType: Record<string, number> = {};
    targets.forEach(t => {
        byType[t.type] = (byType[t.type] || 0) + 1;
    });

    console.log("Summary by type:");
    Object.entries(byType).forEach(([type, count]) => {
        console.log(`  ${type}: ${count}`);
    });
}

async function commandAttach(client: CDPClient, options: CLIOptions): Promise<void> {
    if (!options.targetId) {
        console.error("Error: targetId is required for attach command");
        console.log("Use 'wmpf-cli list' to see available targets");
        process.exit(1);
    }

    console.log(`Attaching to target: ${options.targetId}`);
    const success = await client.attachToTarget(options.targetId);

    if (success) {
        console.log("Successfully attached! You can now use DevTools to inspect this target.");
        console.log(`Navigate to: devtools://devtools/bundled/inspector.html?ws=${options.host}:${options.port}`);
    } else {
        console.error("Failed to attach to target");
        process.exit(1);
    }
}

async function commandAutoAttach(client: CDPClient, options: CLIOptions): Promise<void> {
    let targets = await client.getTargets();

    // Apply filters
    if (options.type) {
        targets = filterTargetsByType(targets, options.type);
    }

    if (options.pattern) {
        const regex = new RegExp(options.pattern, "i");
        targets = targets.filter(t => regex.test(t.title) || regex.test(t.url));
    }

    // Find unattached target
    const target = targets.find(t => !t.attached);

    if (!target) {
        console.log("No available target found. Listing all targets:");
        console.log(formatTargetList(await client.getTargets()));
        process.exit(1);
    }

    console.log(`Auto-attaching to: ${target.title}`);
    console.log(`TargetID: ${target.targetId}`);
    console.log(`Type: ${target.type}`);
    console.log(`URL: ${target.url}`);

    const success = await client.attachToTarget(target.targetId);

    if (success) {
        console.log("\nSuccessfully attached!");
        console.log(`Navigate to: devtools://devtools/bundled/inspector.html?ws=${options.host}:${options.port}`);
    } else {
        console.error("Failed to attach");
        process.exit(1);
    }
}

async function commandWatch(client: CDPClient, options: CLIOptions): Promise<void> {
    console.log("Watching for new targets Press Ctrl+C to stop.");

    const knownTargets = new Set<string>();

    const checkLoop = async () => {
        try {
            const targets = await client.getTargets();

            for (const target of targets) {
                if (!knownTargets.has(target.targetId)) {
                    knownTargets.add(target.targetId);

                    // Auto-attach if matches criteria
                    let shouldAttach = false;

                    if (!options.type && !options.pattern) {
                        shouldAttach = !target.attached;
                    } else {
                        if (options.type && target.type === options.type) {
                            shouldAttach = !target.attached;
                        }
                        if (options.pattern) {
                            const regex = new RegExp(options.pattern, "i");
                            if (regex.test(target.title)) {
                                shouldAttach = !target.attached;
                            }
                        }
                    }

                    if (shouldAttach) {
                        console.log(`\n[NEW] ${target.type}: ${target.title}`);
                        console.log(`      URL: ${target.url}`);
                        console.log(`      TargetID: ${target.targetId}`);

                        await client.attachToTarget(target.targetId);
                        console.log(`      Auto-attached!`);
                    }
                }
            }
        } catch (e) {
            console.error("Error checking targets:", e);
        }
    };

    // Initial check
    await checkLoop();

    // Poll every 2 seconds
    const interval = setInterval(checkLoop, 2000);

    process.on("SIGINT", () => {
        clearInterval(interval);
        console.log("\nStopping watch");
        client.close();
        process.exit(0);
    });
}

async function main() {
    const args = process.argv.slice(2);
    const options = parseArgs(args);

    const url = `ws://${options.host}:${options.port}`;
    const client = new CDPClient(url);

    try {
        await client.connect();

        switch (options.command) {
            case "list":
                await commandList(client, options);
                client.close();
                break;

            case "attach":
                await commandAttach(client, options);
                // Keep connection alive
                process.on("SIGINT", () => {
                    client.close();
                    process.exit(0);
                });
                break;

            case "auto-attach":
                await commandAutoAttach(client, options);
                // Keep connection alive
                process.on("SIGINT", () => {
                    client.close();
                    process.exit(0);
                });
                break;

            case "watch":
                await commandWatch(client, options);
                break;
        }
    } catch (e: any) {
        console.error(`Error: ${e.message}`);
        if (e.message.includes("ECONNREFUSED")) {
            console.error("\nMake sure WMPFDebugger is running:");
            console.error("  npm run start");
        }
        process.exit(1);
    }
}

main();
