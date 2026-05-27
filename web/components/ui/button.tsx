import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center whitespace-nowrap rounded-xl text-sm font-semibold ring-offset-background transition-all duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 active:scale-[0.98]",
  {
    variants: {
      variant: {
        default:   "bg-lex-blue text-white hover:bg-lex-blue-dark shadow-sm",
        secondary: "bg-lex-blue-light text-lex-blue hover:bg-blue-100",
        outline:   "border-2 border-lex-blue text-lex-blue bg-transparent hover:bg-lex-blue-light",
        ghost:     "text-lex-blue hover:bg-lex-blue-light",
        danger:    "bg-red-500 text-white hover:bg-red-600 shadow-sm",
        success:   "bg-lex-green text-white hover:bg-green-600 shadow-sm",
        gold:      "bg-lex-gold text-gray-900 hover:bg-yellow-400 shadow-sm",
      },
      size: {
        sm:   "h-9 px-4 text-xs",
        md:   "h-11 px-6",
        lg:   "h-14 px-8 text-base",
        icon: "h-11 w-11",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
