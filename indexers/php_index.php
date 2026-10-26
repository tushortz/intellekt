<?php
// Index a PHP install.
//
//   php php_index.php --out symbols.jsonl
//   php php_index.php --lib ./some/dir --out symbols.jsonl
//
// The full run uses Reflection on loaded core and extensions. --lib only
// scans the .php files in that directory (the check script).
//
// NOTE: Composer packages are read from COMPOSER_HOME/vendor when that
// directory exists, and the walk stops after MAX_FILES. A project's vendor
// directory is not indexed. Upgrade path: index the active view's folder.

const MAX_FILES = 2000;

function clip($text)
{
    $text = str_replace(array("\r\n", "\r"), "\n", (string) $text);
    return trim($text);
}

function snippet($name, $labels)
{
    $parts = array();
    $index = 1;
    foreach ($labels as $label) {
        $label = ltrim((string) $label, '$&');
        if ($label === '') {
            continue;
        }
        $parts[] = '${' . $index . ':' . $label . '}';
        $index++;
    }
    if (!$parts) {
        return $name . '()';
    }
    return $name . '(' . implode(', ', $parts) . ')';
}

function clean_doc($raw)
{
    $raw = preg_replace('#^/\*\*#', '', (string) $raw);
    $raw = preg_replace('#\*/$#', '', $raw);
    $lines = array();
    foreach (preg_split('/\R/', $raw) as $line) {
        $lines[] = preg_replace('/^\s*\*\s?/', '', $line);
    }
    return clip(implode("\n", $lines));
}

$outPath = '';
$lib = '';
for ($i = 1; $i < count($argv); $i++) {
    if ($argv[$i] === '--out') {
        $outPath = $argv[$i + 1] ?? '';
        $i++;
    } elseif ($argv[$i] === '--lib') {
        $lib = $argv[$i + 1] ?? '';
        $i++;
    }
}
if ($outPath === '') {
    fwrite(STDERR, "missing --out\n");
    exit(1);
}
$handle = fopen($outPath, 'w');
if ($handle === false) {
    fwrite(STDERR, "cannot write output\n");
    exit(1);
}

function emit($handle, $rec)
{
    fwrite($handle, json_encode($rec, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . "\n");
}

function return_name($fn)
{
    // ReflectionUnionType and ReflectionIntersectionType have no getName().
    // A union is not a single owner, so it is left blank.
    if (!method_exists($fn, 'getReturnType')) {
        return '';
    }
    try {
        $type = $fn->getReturnType();
    } catch (Throwable $err) {
        return '';
    }
    if ($type instanceof ReflectionNamedType) {
        return $type->getName();
    }
    return '';
}

function scan_text($handle, $text)
{
    $namespace = '';
    if (preg_match('/^\s*namespace\s+([\w\\\\]+)\s*;/m', $text, $found)) {
        $namespace = $found[1];
    }
    $brace = 0;
    $class = '';
    $classBrace = -1;
    $want = '';
    $pending = '';
    foreach (preg_split('/\R/', $text) as $line) {
        if ($class !== '' && $brace <= $classBrace) {
            $class = '';
        }
        $trim = trim($line);
        if (strpos($trim, '/**') !== false) {
            $pending = $trim;
            if (strpos($trim, '*/') === false) {
                continue;
            }
        } elseif ($pending !== '' && strpos($pending, '*/') === false) {
            $pending .= ' ' . $trim;
            if (strpos($trim, '*/') === false) {
                continue;
            }
        }
        if (preg_match('/\b(?:final\s+|abstract\s+)?class\s+(\w+)/', $trim, $found)) {
            $want = $namespace === '' ? $found[1] : $namespace . '\\' . $found[1];
        }
        $open = substr_count($line, '{');
        if ($want !== '' && $open > 0) {
            $class = $want;
            $classBrace = $brace;
            $want = '';
            emit($handle, array('t' => 'm', 'name' => $class, 'doc' => clean_doc($pending)));
            $pending = '';
        }
        if (preg_match('/\bfunction\s+&?(\w+)\s*\(([^)]*)\)/', $trim, $found)) {
            if (strpos($trim, 'private ') !== false || strpos($trim, 'protected ') !== false) {
                $pending = '';
            } else {
                $labels = array();
                foreach (explode(',', $found[2]) as $part) {
                    if (preg_match('/(\$\w+)/', $part, $var)) {
                        $labels[] = $var[1];
                    }
                }
                $owner = $class !== '' ? $class : 'php';
                emit($handle, array(
                    't' => 's',
                    'owner' => $owner,
                    'name' => $found[1],
                    'kind' => 'function',
                    'signature' => snippet($found[1], $labels),
                    'doc' => clean_doc($pending),
                    'returns' => '',
                ));
                $pending = '';
            }
        }
        $brace += $open - substr_count($line, '}');
        if ($brace < 0) {
            $brace = 0;
        }
    }
}

function scan_tree($handle, $dir, &$seen)
{
    if ($dir === '' || !is_dir($dir)) {
        return;
    }
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($dir, FilesystemIterator::SKIP_DOTS)
    );
    foreach ($iterator as $file) {
        if ($seen >= MAX_FILES) {
            return;
        }
        $path = $file->getPathname();
        if (substr($path, -4) !== '.php') {
            continue;
        }
        $rel = substr($path, strlen(rtrim($dir, '/\\')));
        if (preg_match('#[\\\\/](test|tests|testing)[\\\\/]#', $rel)) {
            continue;
        }
        $text = file_get_contents($path);
        if ($text === false) {
            continue;
        }
        $seen++;
        scan_text($handle, $text);
    }
}

if ($lib !== '') {
    $seen = 0;
    scan_tree($handle, $lib, $seen);
    fclose($handle);
    exit(0);
}

foreach (get_defined_functions()['internal'] as $name) {
    try {
        $func = new ReflectionFunction($name);
    } catch (Throwable $err) {
        continue;
    }
    $labels = array();
    foreach ($func->getParameters() as $param) {
        $labels[] = $param->getName();
    }
    emit($handle, array(
        't' => 's',
        'owner' => 'php',
        'name' => $name,
        'kind' => 'function',
        'signature' => snippet($name, $labels),
        'doc' => clean_doc($func->getDocComment() ?: ''),
        'returns' => return_name($func),
    ));
}

foreach (get_declared_classes() as $class) {
    try {
        $ref = new ReflectionClass($class);
    } catch (Throwable $err) {
        continue;
    }
    if (!$ref->isInternal()) {
        continue;
    }
    emit($handle, array('t' => 'm', 'name' => $class, 'doc' => clean_doc($ref->getDocComment() ?: '')));
    foreach ($ref->getMethods(ReflectionMethod::IS_PUBLIC) as $method) {
        if (strpos($method->getName(), '__') === 0) {
            continue;
        }
        $labels = array();
        foreach ($method->getParameters() as $param) {
            $labels[] = $param->getName();
        }
        emit($handle, array(
            't' => 's',
            'owner' => $class,
            'name' => $method->getName(),
            'kind' => 'function',
            'signature' => snippet($method->getName(), $labels),
            'doc' => clean_doc($method->getDocComment() ?: ''),
            'returns' => return_name($method),
        ));
    }
}

$composer = getenv('COMPOSER_HOME');
if (!$composer) {
    $home = getenv('HOME') ?: '';
    if ($home !== '' && is_dir($home . '/.config/composer/vendor')) {
        $composer = $home . '/.config/composer';
    } elseif ($home !== '' && is_dir($home . '/.composer/vendor')) {
        $composer = $home . '/.composer';
    }
}
if ($composer && is_dir($composer . '/vendor')) {
    $seen = 0;
    scan_tree($handle, $composer . '/vendor', $seen);
}

fclose($handle);
exit(0);
