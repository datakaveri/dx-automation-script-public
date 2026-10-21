declare module "newman" {
  const newman: {
    run(
      options: unknown,
      callback: (error: Error | null, summary: unknown) => void
    ): { on(event: string, listener: (error: Error | null, args: any) => void): unknown };
  };
  export default newman;
}
