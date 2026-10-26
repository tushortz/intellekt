# frozen_string_literal: true

# Index Ruby without loading the libraries being scanned.
#
#   ruby ruby_index.rb --out symbols.jsonl
#   ruby ruby_index.rb --lib ./some/tree --out symbols.jsonl
#
# --lib only parses that tree (used by the check script). Otherwise the
# stdlib and installed gems are parsed with Ripper, and the local RI store
# supplies docs plus methods that exist only as C (String, Array, ...).
#
# NOTE: Ripper records method names and parameters. It does not evaluate
# metaprogramming (define_method, method_missing, module_function).
# NOTE: the walk stops after MAX_FILES and skips test directories. RI stops
# after MAX_RI methods. Upgrade path: index one gem at a time.

require "ripper"
require "json"
require "rbconfig"

MAX_FILES = 8000
MAX_RI = 30_000
SKIP_DIRS = %w[test tests testing spec fixtures].freeze

def clip(text)
  text.to_s.gsub(/\s+/, " ").strip[0, 500]
end

def snippet_label(text)
  text.to_s.gsub(/[{}$\\]/) { |char| "\\#{char}" }
end

def placeholder_list(inner)
  parts = []
  buf = +""
  depth = 0
  inner.to_s.each_char do |char|
    if char == "(" || char == "[" || char == "{"
      depth += 1
      buf << char
    elsif char == ")" || char == "]" || char == "}"
      depth -= 1 if depth.positive?
      buf << char
    elsif char == "," && depth.zero?
      parts << buf.strip
      buf = +""
    else
      buf << char
    end
  end
  parts << buf.strip unless buf.strip.empty?
  parts.reject!(&:empty?)
  return "" if parts.empty?

  parts.each_with_index.map { |part, index|
    "${#{index + 1}:#{snippet_label(part)}}"
  }.join(", ")
end

def ident_text(node)
  return nil unless node.is_a?(Array)

  case node[0]
  when :@ident, :@const
    node[1].to_s
  when :@label
    node[1].to_s.sub(/:\z/, "")
  when :const_ref, :var_ref, :top_const_ref
    ident_text(node[1])
  when :const_path_ref
    left = ident_text(node[1])
    right = ident_text(node[2])
    return nil unless left && right

    "#{left}::#{right}"
  else
    node.each do |child|
      next unless child.is_a?(Array)

      found = ident_text(child)
      return found if found
    end
    nil
  end
end

def unwrap_params(node)
  return nil unless node.is_a?(Array)
  return unwrap_params(node[1]) if node[0] == :paren
  return node if node[0] == :params

  nil
end

def param_names(params)
  return [] unless params.is_a?(Array) && params[0] == :params

  names = []
  Array(params[1]).each { |node| text = ident_text(node) and names << text }
  Array(params[2]).each do |pair|
    text = ident_text(pair.is_a?(Array) ? pair[0] : pair)
    names << text if text
  end
  rest = params[3]
  if rest.is_a?(Array)
    text = ident_text(rest)
    names << (text ? "*#{text}" : "*args")
  end
  Array(params[5]).each do |pair|
    text = ident_text(pair.is_a?(Array) ? pair[0] : pair)
    names << text if text
  end
  kwrest = params[6]
  if kwrest.is_a?(Array)
    text = ident_text(kwrest)
    names << (text ? "**#{text}" : "**kwargs")
  end
  names.compact
end

def signature_for(name, params, _singleton)
  # Inserted text is the call, so class methods are not prefixed with "self.".
  "#{name}(#{placeholder_list_from_names(param_names(params))})"
end

def placeholder_list_from_names(names)
  names.each_with_index.map { |part, index|
    "${#{index + 1}:#{snippet_label(part)}}"
  }.join(", ")
end

def comments_by_line(src)
  pending = []
  attached = {}
  Ripper.lex(src).each do |pos, type, text, _state|
    line = pos[0]
    case type
    when :on_comment
      pending << text.to_s.sub(/\A#\s?/, "").strip
    when :on_sp, :on_nl, :on_ignored_nl
      next
    else
      if pending.any?
        attached[line] = clip(pending.join(" "))
        pending = []
      end
    end
  end
  attached
end

def line_of(node)
  return nil unless node.is_a?(Array)

  pos = node[2]
  pos.is_a?(Array) ? pos[0] : nil
end

def walk(node, stack, comments, out, seen_modules)
  return unless node.is_a?(Array)

  case node[0]
  when :class, :module
    raw = ident_text(node[1])
    return unless raw

    fq = if raw.include?("::") || stack.empty?
           raw
         else
           "#{stack.last}::#{raw}"
         end
    unless seen_modules[fq]
      out << { "t" => "m", "name" => fq, "doc" => "" }
      seen_modules[fq] = true
    end
    body = node[0] == :class ? node[3] : node[2]
    walk(body, stack + [fq], comments, out, seen_modules)
  when :sclass
    walk(node[2], stack, comments, out, seen_modules)
  when :def
    emit_method(node[1], node[2], false, stack, comments, out)
  when :defs
    emit_method(node[3], node[4], true, stack, comments, out)
  else
    node.each { |child| walk(child, stack, comments, out, seen_modules) }
  end
end

def emit_method(ident, params_node, singleton, stack, comments, out)
  name = ident_text(ident)
  return unless name

  owner = stack.last || "Object"
  line = line_of(ident)
  out << {
    "t" => "s",
    "owner" => owner,
    "name" => name,
    "kind" => "function",
    "signature" => signature_for(name, unwrap_params(params_node), singleton),
    "doc" => (line && comments[line]).to_s
  }
end

def index_file(root, path, out, seen_modules, seen_requires)
  src = File.read(path, encoding: "UTF-8", invalid: :replace, undef: :replace)
  sexp = Ripper.sexp(src)
  return unless sexp

  rel = path.sub(/\A#{Regexp.escape(root)}#{Regexp.escape(File::SEPARATOR)}?/, "")
  rel = rel.sub(/\.rb\z/, "")
  unless rel.empty? || seen_requires[rel]
    out << { "t" => "m", "name" => rel, "doc" => "" }
    seen_requires[rel] = true
  end
  walk(sexp, [], comments_by_line(src), out, seen_modules)
rescue SystemCallError, StandardError
  nil
end

def collect(root, acc)
  return unless root && Dir.exist?(root)
  return if acc.size >= MAX_FILES

  prefix = "#{File.expand_path(root)}#{File::SEPARATOR}"
  Dir.glob(File.join(root, "**", "*.rb")).each do |path|
    rel = path.start_with?(prefix) ? path[prefix.length..-1] : path
    next if rel.split(File::SEPARATOR).any? { |part| SKIP_DIRS.include?(part) }

    acc << [root, path]
    return if acc.size >= MAX_FILES
  end
end

def ri_text(comment)
  return "" unless comment

  doc = comment.instance_variable_get(:@document)
  return "" unless doc.respond_to?(:parts)

  doc.parts.each do |part|
    next unless part.class.name.end_with?("Paragraph") && part.respond_to?(:parts)

    return clip(part.parts.join(" "))
  end
  ""
rescue StandardError
  ""
end

def snippet_from_ri(name, call_seq)
  line = call_seq.to_s.lines.map(&:strip).find { |row| row.start_with?(name) && row.include?("(") }
  if line
    expr = line.split("->", 2).first.split("{", 2).first.strip
    if expr =~ /\A#{Regexp.escape(name)}\((.*)\)\s*\z/
      return "#{name}(#{placeholder_list(Regexp.last_match(1))})"
    end
  end
  "#{name}()"
end

def index_ri(out, seen_pairs)
  verbose = $VERBOSE
  $VERBOSE = nil
  require "rdoc"
  require "rdoc/ri/paths"
  paths = RDoc::RI::Paths.path(true, true, true, true)
  loaded = 0
  paths.each do |path|
    next unless path && Dir.exist?(path)

    store = RDoc::Store.new(RDoc::Options.new, path: path)
    store.load_cache
    {
      false => store.cache[:instance_methods] || {},
      true => store.cache[:class_methods] || {}
    }.each do |singleton, table|
      table.each do |klass, methods|
        klass = klass.to_s
        unless seen_pairs[[klass, nil]]
          rec = { "t" => "m", "name" => klass, "doc" => "" }
          out << rec
          seen_pairs[[klass, nil]] = rec
        end
        Array(methods).each do |method_name|
          break if loaded >= MAX_RI

          sig_name = method_name.to_s
          key = [klass, sig_name]
          existing = seen_pairs[key]
          if existing && !(existing.is_a?(Hash) && existing["doc"].to_s.empty?)
            next
          end
          lookup = singleton ? sig_name : "##{sig_name}"
          begin
            method = store.load_method(klass, lookup)
          rescue StandardError
            next
          end
          loaded += 1
          doc = ri_text(method.comment)
          if existing.is_a?(Hash)
            existing["doc"] = doc if existing["doc"].to_s.empty? && !doc.empty?
            next
          end
          rec = {
            "t" => "s",
            "owner" => klass,
            "name" => sig_name,
            "kind" => "function",
            "signature" => snippet_from_ri(sig_name, method.call_seq),
            "doc" => doc
          }
          out << rec
          seen_pairs[key] = rec
        end
      end
    end
  end
rescue LoadError, StandardError
  nil
ensure
  $VERBOSE = verbose if defined?(verbose)
end

def build(lib)
  out = []
  modules = {}
  requires = {}
  files = []
  if lib
    collect(File.expand_path(lib), files)
    files.each { |root, path| index_file(root, path, out, modules, requires) }
    return out
  end

  libdir = RbConfig::CONFIG["rubylibdir"]
  collect(libdir, files)
  begin
    require "rubygems"
    Gem::Specification.latest_specs.each do |spec|
      spec.require_paths.each do |rel|
        collect(File.join(spec.full_gem_path, rel), files)
      end
    end
  rescue LoadError
    nil
  end
  files.each { |root, path| index_file(root, path, out, modules, requires) }

  # Ripper signatures win. RI fills an empty doc and adds C methods Ripper
  # never saw (String, Array, and the rest of the core).
  seen_pairs = {}
  out.each do |rec|
    if rec["t"] == "s"
      seen_pairs[[rec["owner"], rec["name"]]] = rec
    elsif rec["t"] == "m"
      seen_pairs[[rec["name"], nil]] = rec
    end
  end
  index_ri(out, seen_pairs)
  out
end

def main(argv)
  out_path = nil
  lib = nil
  args = argv.dup
  while (arg = args.shift)
    case arg
    when "--out"
      out_path = args.shift
    when "--lib"
      lib = args.shift
    end
  end
  unless out_path
    warn "usage: ruby_index.rb --out FILE [--lib DIR]"
    return 2
  end
  records = build(lib)
  File.open(out_path, "w:UTF-8") do |file|
    records.each { |rec| file.puts(JSON.generate(rec)) }
  end
  warn "indexed #{records.size} records"
  0
end

exit main(ARGV) if $PROGRAM_NAME == __FILE__
