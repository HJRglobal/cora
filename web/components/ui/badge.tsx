import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold transition-colors",
  {
    variants: {
      variant: {
        open:      "bg-lex-blue-light text-lex-blue",
        matched:   "bg-lex-gold-light text-yellow-800",
        confirmed: "bg-lex-green-light text-green-800",
        cancelled: "bg-red-50 text-red-700",
        expired:   "bg-gray-100 text-gray-500",
        admin:     "bg-lex-purple-light text-purple-800",
        default:   "bg-gray-100 text-gray-600",
      },
    },
    defaultVariants: { variant: "default" },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
