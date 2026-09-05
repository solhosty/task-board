import * as React from 'react';
import { Slot } from 'radix-ui';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';
const buttonVariants=cva('button',{variants:{variant:{default:'primary',secondary:'secondary',ghost:'quiet',destructive:'destructive',outline:'secondary'},size:{default:'',sm:'small',icon:'icon-button'}},defaultVariants:{variant:'default',size:'default'}});
export function Button({className,variant,size,asChild=false,...props}:React.ComponentProps<'button'>&VariantProps<typeof buttonVariants>&{asChild?:boolean}){const Comp=asChild?Slot.Root:'button';return <Comp className={cn(buttonVariants({variant,size,className}))} {...props}/>;}
export {buttonVariants};
