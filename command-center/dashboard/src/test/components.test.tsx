import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Findings, Markdown, Pill, RiskBadge, timeAgo } from "../components";

describe("timeAgo", () => {
  const now = Date.parse("2026-10-18T12:00:00Z");
  it("formats past and future", () => {
    expect(timeAgo(null)).toBe("never");
    expect(timeAgo("2026-10-18T11:59:50Z", now)).toBe("just now");
    expect(timeAgo("2026-10-18T11:50:00Z", now)).toBe("10 min ago");
    expect(timeAgo("2026-10-18T09:00:00Z", now)).toBe("3 h ago");
    expect(timeAgo("2026-10-16T12:00:00Z", now)).toBe("2 d ago");
    expect(timeAgo("2026-10-18T12:15:00Z", now)).toBe("in 15 min");
  });
});

describe("Markdown", () => {
  it("renders headings, lists and bold", () => {
    render(<Markdown text={"# Daily briefing\n\n- **Acme** is down\n- ok\n\nA paragraph."} />);
    expect(screen.getByRole("heading", { name: "Daily briefing" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("Acme").tagName).toBe("STRONG");
  });

  it("never interprets HTML from agent output", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"> <script>alert(2)</script>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });
});

describe("badges", () => {
  it("labels risk and status", () => {
    render(
      <>
        <RiskBadge risk="external" />
        <Pill value="awaiting_approval" />
      </>,
    );
    expect(screen.getByText("external · approval")).toBeInTheDocument();
    expect(screen.getByText("awaiting approval")).toHaveClass("tone-warn");
  });

  it("orders findings by severity and hides info by default", () => {
    render(
      <Findings
        findings={[
          { code: "a", severity: "info", title: "Info thing" },
          { code: "b", severity: "warning", title: "Warn thing" },
          { code: "c", severity: "critical", title: "Critical thing" },
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual([expect.stringContaining("Critical thing"), expect.stringContaining("Warn thing")]);
  });
});
