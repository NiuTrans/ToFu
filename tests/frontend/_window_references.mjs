/** Resolve actual browser-window property references using TypeScript binding.
 * Input is a JSON filename/source map on stdin; output is filename/property lists.
 * Local parameters, variables, object members, strings and comments are excluded.
 */
import fs from 'node:fs';
import ts from 'typescript';

const sources = JSON.parse(fs.readFileSync(0, 'utf8'));
const options = { allowJs: true, noLib: true, noResolve: true, target: ts.ScriptTarget.Latest };
const host = ts.createCompilerHost(options);
host.fileExists = (name) => Object.hasOwn(sources, name);
host.readFile = (name) => sources[name];
host.getSourceFile = (name) => host.fileExists(name)
  ? ts.createSourceFile(name, sources[name], options.target, true) : undefined;
const program = ts.createProgram(Object.keys(sources), options, host);
const checker = program.getTypeChecker();
const references = {};
for (const name of Object.keys(sources)) {
  const properties = new Set();
  const source = program.getSourceFile(name);
  if (!source) throw new Error(`Missing source ${name}`);
  function visit(node) {
    if (ts.isPropertyAccessExpression(node)
        && ts.isIdentifier(node.expression) && node.expression.text === 'window') {
      const symbol = checker.getSymbolAtLocation(node.expression);
      if (!symbol?.declarations?.some((declaration) => !declaration.getSourceFile().isDeclarationFile)) {
        properties.add(node.name.text);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  references[name] = [...properties].sort();
}
// Browser-native members come from the locked DOM declarations, not a manual
// list that falls behind legitimate uses of browser APIs.
const nativeProgram = ts.createProgram([ts.getDefaultLibFilePath({})], {});
const nativeChecker = nativeProgram.getTypeChecker();
const dom = nativeProgram.getSourceFiles().find((source) => source.fileName.endsWith('/lib.dom.d.ts'));
const windowDeclaration = dom?.statements.filter(ts.isVariableStatement)
  .flatMap((statement) => [...statement.declarationList.declarations])
  .find((declaration) => ts.isIdentifier(declaration.name) && declaration.name.text === 'window');
if (!windowDeclaration) throw new Error('TypeScript DOM declarations do not define window');
const native = nativeChecker.getPropertiesOfType(nativeChecker.getTypeAtLocation(windowDeclaration.name))
  .map((property) => property.name);
process.stdout.write(JSON.stringify({ references, native }));
