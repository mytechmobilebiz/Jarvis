import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import pc from "picocolors";
import { readYesNo } from "./answers.js";
import { SetupCancelledError, type StepOutcome, type StepStatus } from "./context.js";
import { greetingLine } from "./ui.js";
import type { CommandStream, DeviceCodePrompt, SecretPromptGuide, SetupChoice, SetupIO } from "./io.js";

export function banner(): void {
  console.log("");
  console.log(pc.bold(pc.cyan("JARVIS")));
  console.log(pc.gray("Self-hosted AI chief-of-staff kit"));
  console.log(pc.gray("Hermes Agent profile distribution setup"));
  console.log("");
  console.log(pc.white(greetingLine()));
  console.log("");
}

export function stepHeading(index: number, total: number, title: string): void {
  console.log("");
  console.log(pc.bold(pc.cyan(`${index}/${total} ${title}`)));
}

export function success(message: string): void {
  console.log(`  ${pc.green(message)}`);
}

export function errorLine(message: string): void {
  console.log(`  ${pc.red(message)}`);
}

export function printOutcome(io: SetupIO, outcome: StepOutcome): void {
  const label =
    outcome.status === "done" ? pc.green("done") : outcome.status === "skipped" ? pc.yellow("skipped") : pc.red("failed");
  success(`Result: ${label}`);
  for (const line of outcome.notes) {
    if (outcome.status === "failed") errorLine(line);
    else io.note(line);
  }
}

export interface TerminalIOOptions {
  /**
   * Called when Ctrl+C is read at a raw-mode prompt. The CLI wires this to the
   * same abort the OS signal handler uses, so both routes end the run
   * identically instead of one of them surfacing as an ordinary failure.
   */
  onCancel?: () => void;
  /** Injected in tests so the raw-mode paths are exercised without a TTY. */
  input?: NodeJS.ReadStream;
  output?: NodeJS.WriteStream;
}

export class TerminalIO implements SetupIO {
  readonly inheritsStdio = true;
  private readonly onCancel: () => void;
  private readonly input: NodeJS.ReadStream;
  private readonly out: NodeJS.WriteStream;

  constructor(options: TerminalIOOptions = {}) {
    this.onCancel = options.onCancel ?? ((): void => {});
    this.input = options.input ?? input;
    this.out = options.output ?? (output as NodeJS.WriteStream);
  }

  /** The raw-mode reader's view of this IO: streams plus the cancel hook. */
  private keyReader(): KeyReaderIO {
    return { onCancel: this.onCancel, input: this.input, out: this.out };
  }

  async confirm(message: string, defaultValue: boolean): Promise<boolean> {
    const suffix = defaultValue ? "[Y/n]" : "[y/N]";
    const answer = await question(`${message} ${pc.gray(suffix)} `);
    return readYesNo(answer) ?? defaultValue;
  }

  async select(message: string, choices: SetupChoice[], defaultKey: string): Promise<string> {
    console.log(message);
    for (const choice of choices) {
      console.log(`  ${pc.cyan(choice.hotkey)} ${choice.label} ${pc.gray(choice.description)}`);
    }
    const key = await readSingleKey(`${pc.gray("Choose one key, or Enter for the default.")} `, this.keyReader());
    console.log("");
    if (key === "\r" || key === "\n" || key === "") return defaultKey;
    const found = choices.find((choice) => choice.hotkey.toLowerCase() === key.toLowerCase());
    return found?.key ?? defaultKey;
  }

  async promptText(message: string, defaultValue: string, guide?: SecretPromptGuide): Promise<string> {
    printGuide(guide);
    const suffix = defaultValue === "" ? ` ${pc.gray("[blank to skip]")}` : ` ${pc.gray(`(${defaultValue})`)}`;
    const answer = await question(`${message}${suffix} `);
    return answer.trim() === "" ? defaultValue : answer.trim();
  }

  async promptSecret(message: string, guide?: SecretPromptGuide): Promise<string> {
    // Printed before the prompt, not after: a customer who does not have a key
    // yet needs the guide while the cursor is still waiting, not once they have
    // already given up on the step.
    printGuide(guide);

    const stream = this.input as NodeJS.ReadStream & { setRawMode?: (mode: boolean) => void };
    if (!this.input.isTTY || typeof stream.setRawMode !== "function") {
      return question(`${message} `);
    }

    this.out.write(`${message} ${pc.gray("[blank to skip]")} `);
    stream.setRawMode(true);
    this.input.resume();

    return new Promise<string>((resolve, reject) => {
      let value = "";

      const cleanup = (): void => {
        stream.setRawMode?.(false);
        this.input.off("data", onData);
        this.out.write("\n");
      };

      const onData = (chunk: Buffer): void => {
        const text = chunk.toString("utf8");
        for (const char of text) {
          if (char === "\u0003") {
            cleanup();
            // A cancellation, not a failure: this ends the run through the same
            // path an OS SIGINT would, instead of looking like a broken step.
            this.onCancel();
            reject(new SetupCancelledError());
            return;
          }
          if (char === "\r" || char === "\n") {
            cleanup();
            resolve(value);
            return;
          }
          if (char === "\u007f" || char === "\b") {
            if (value.length > 0) {
              value = value.slice(0, -1);
              this.out.write("\b \b");
            }
            continue;
          }
          value += char;
          this.out.write("*");
        }
      };

      this.input.on("data", onData);
    });
  }

  deviceCode(prompt: DeviceCodePrompt): void {
    console.log("");
    if (prompt.status === "waiting") {
      console.log(`  ${pc.white(prompt.message)}`);
      console.log(`  ${pc.gray("Open:")} ${pc.cyan(prompt.url ?? "")}`);
      console.log(`  ${pc.gray("Code:")} ${pc.bold(pc.cyan(prompt.code ?? ""))}`);
      console.log(`  ${pc.gray("Leave this window open; I finish as soon as the page says you are signed in.")}`);
    } else {
      // The code printed a moment ago is dead now, so the terminal has to say
      // so — otherwise a customer keeps typing an expired code into a browser.
      const paint = prompt.status === "signed-in" ? pc.green : pc.yellow;
      console.log(`  ${paint(prompt.message)}`);
    }
    console.log("");
  }

  note(text: string): void {
    console.log(`  ${pc.gray(text)}`);
  }

  warn(text: string): void {
    console.log(`  ${pc.yellow(text)}`);
  }

  error(text: string): void {
    errorLine(text);
  }

  command(_text: string): void {
    // Terminal runs inherit stdio, so the command and its output are already visible.
  }

  commandOutput(chunk: string, stream: CommandStream): void {
    if (stream === "stderr") process.stderr.write(chunk);
    else output.write(chunk);
  }

  block(title: string, lines: string[]): void {
    console.log("");
    console.log(pc.bold(title));
    for (const line of lines) {
      console.log(line);
    }
  }
}

function printGuide(guide?: SecretPromptGuide): void {
  if (guide === undefined) return;
  const link = guide.linkText === undefined ? "" : ` ${guide.linkText}`;
  console.log(pc.gray(`${guide.text}${link}`));
}

async function question(prompt: string): Promise<string> {
  const rl = createInterface({ input, output });
  try {
    return await rl.question(prompt);
  } finally {
    rl.close();
  }
}

interface KeyReaderIO {
  onCancel: () => void;
  input: NodeJS.ReadStream;
  out: NodeJS.WriteStream;
}

async function readSingleKey(prompt: string, io?: KeyReaderIO): Promise<string> {
  const source = io?.input ?? input;
  const sink = io?.out ?? (output as NodeJS.WriteStream);
  const stream = source as NodeJS.ReadStream & { setRawMode?: (mode: boolean) => void };
  if (!source.isTTY || typeof stream.setRawMode !== "function") {
    const answer = await question(prompt);
    return answer.slice(0, 1);
  }

  sink.write(prompt);
  stream.setRawMode(true);
  source.resume();

  return new Promise<string>((resolve, reject) => {
    const cleanup = (): void => {
      stream.setRawMode?.(false);
      source.off("data", onData);
    };

    const onData = (chunk: Buffer): void => {
      const char = chunk.toString("utf8").slice(0, 1);
      if (char === "\u0003") {
        cleanup();
        io?.onCancel();
        reject(new SetupCancelledError());
        return;
      }
      cleanup();
      resolve(char);
    };

    source.on("data", onData);
  });
}
