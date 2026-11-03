/** 合并 className，过滤空值（对应 minimal-shared 的 mergeClasses，去掉外部依赖）。 */
export function mergeClasses(classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(' ');
}
