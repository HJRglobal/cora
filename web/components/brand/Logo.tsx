import Image from "next/image";
import { cn } from "@/lib/utils";

interface LogoProps {
  variant?: "full" | "wordmark" | "icon";
  className?: string;
  size?: "sm" | "md" | "lg";
}

const sizes = {
  sm: { icon: 28, wordmark: 100 },
  md: { icon: 40, wordmark: 140 },
  lg: { icon: 56, wordmark: 200 },
};

export function Logo({ variant = "full", size = "md", className }: LogoProps) {
  const s = sizes[size];

  if (variant === "wordmark") {
    return (
      <span
        className={cn("font-bold tracking-tight text-lex-blue", className)}
        style={{ fontSize: s.wordmark * 0.25 }}
      >
        Lexington
      </span>
    );
  }

  // Inline SVG cube mark that matches the logo exactly
  const CubeMark = () => (
    <svg
      width={s.icon}
      height={s.icon}
      viewBox="0 0 100 100"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* Purple — top face */}
      <polygon points="50,5 90,25 50,45 10,25" fill="#8B44AC" />
      {/* Gold — left face */}
      <polygon points="10,25 50,45 50,90 10,65" fill="#FAC119" />
      {/* Cyan/Blue — L shape right face */}
      <polygon points="50,45 90,25 90,65 50,90" fill="#29ABE2" />
      {/* Green — inner top-right highlight */}
      <polygon points="70,35 90,25 90,45 70,55" fill="#8DC63F" />
    </svg>
  );

  if (variant === "icon") return <CubeMark />;

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <CubeMark />
      <span
        className="font-bold text-lex-blue leading-none"
        style={{ fontSize: s.wordmark * 0.22 }}
      >
        Lexington
      </span>
    </div>
  );
}
